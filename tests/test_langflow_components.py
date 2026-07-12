from __future__ import annotations

import ast
import builtins
import asyncio
import importlib.util
import json
import sys
import types
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_FILES = sorted(
    path
    for path in (ROOT / "langflow_components").glob("*/*.py")
    if not path.name.endswith("_input_example.py")
)


def install_lfx_test_stubs() -> None:
    if "lfx" not in sys.modules and importlib.util.find_spec("lfx") is not None:
        return

    class Component:
        pass

    class RunFlowBaseComponent(Component):
        IOPUT_SEP = "~"

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(self, text=""):
            self.text = text

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class BoolInput(InputBase):
        pass

    class SecretStrInput(InputBase):
        pass

    class AgentComponent(Component):
        inputs = []
        outputs = [InputBase(name="response", method="message_response", types=["Message"])]

        async def message_response(self):
            return Message(text=getattr(self, "_stub_response", "stub agent response"))

    modules = {
        "lfx": types.ModuleType("lfx"),
        "lfx.custom": types.ModuleType("lfx.custom"),
        "lfx.custom.custom_component": types.ModuleType("lfx.custom.custom_component"),
        "lfx.custom.custom_component.component": types.ModuleType("lfx.custom.custom_component.component"),
        "lfx.base": types.ModuleType("lfx.base"),
        "lfx.base.tools": types.ModuleType("lfx.base.tools"),
        "lfx.base.tools.run_flow": types.ModuleType("lfx.base.tools.run_flow"),
        "lfx.components": types.ModuleType("lfx.components"),
        "lfx.components.models_and_agents": types.ModuleType("lfx.components.models_and_agents"),
        "lfx.components.models_and_agents.agent": types.ModuleType("lfx.components.models_and_agents.agent"),
        "lfx.io": types.ModuleType("lfx.io"),
        "lfx.schema": types.ModuleType("lfx.schema"),
        "lfx.schema.data": types.ModuleType("lfx.schema.data"),
        "lfx.schema.message": types.ModuleType("lfx.schema.message"),
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)
    sys.modules["lfx.custom.custom_component.component"].Component = getattr(
        sys.modules["lfx.custom.custom_component.component"], "Component", Component
    )
    sys.modules["lfx.custom.custom_component.component"].get_component_toolkit = lambda: None
    sys.modules["lfx.base.tools.run_flow"].RunFlowBaseComponent = RunFlowBaseComponent
    sys.modules["lfx.components.models_and_agents.agent"].AgentComponent = AgentComponent
    io_module = sys.modules["lfx.io"]
    io_module.BoolInput = getattr(io_module, "BoolInput", BoolInput)
    io_module.DataInput = getattr(io_module, "DataInput", InputBase)
    io_module.DropdownInput = getattr(io_module, "DropdownInput", InputBase)
    io_module.MessageInput = getattr(io_module, "MessageInput", InputBase)
    io_module.MessageTextInput = getattr(io_module, "MessageTextInput", InputBase)
    io_module.ModelInput = getattr(io_module, "ModelInput", InputBase)
    io_module.MultilineInput = getattr(io_module, "MultilineInput", InputBase)
    io_module.SecretStrInput = getattr(io_module, "SecretStrInput", SecretStrInput)
    io_module.StrInput = getattr(io_module, "StrInput", InputBase)
    io_module.Output = getattr(io_module, "Output", InputBase)
    sys.modules["lfx.schema.data"].Data = getattr(sys.modules["lfx.schema.data"], "Data", Data)
    sys.modules["lfx.schema.message"].Message = getattr(sys.modules["lfx.schema.message"], "Message", Message)


install_lfx_test_stubs()


def function_case_source(*function_names: str) -> str:
    source = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "function_case_helper_code_input_example.py"
    ).read_text(encoding="utf-8")
    if not function_names:
        return source
    tree = ast.parse(source)
    source_lines = source.splitlines()
    blocks = []
    requested = set(function_names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in requested:
            blocks.append("\n".join(source_lines[node.lineno - 1 : node.end_lineno]))
    return "\n\n".join(blocks)


def install_fake_pymongo(monkeypatch):
    store = {}
    metrics = {"client_count": 0}

    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
            self.limit_value = None

        def limit(self, value):
            self.limit_value = int(value)
            return self

        def __iter__(self):
            docs = self.docs[: self.limit_value] if self.limit_value is not None else self.docs
            return iter(deepcopy(docs))

    class FakeCollection:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query=None, projection=None):
            query = query or {}
            rows = [self._project(doc, projection) for doc in self.docs.values() if self._matches(doc, query)]
            return FakeCursor(rows)

        def find_one(self, query=None, projection=None):
            query = query or {}
            for doc in self.docs.values():
                if self._matches(doc, query):
                    return self._project(doc, projection)
            return None

        def replace_one(self, query, doc, upsert=False):
            doc_id = query.get("_id") or doc.get("_id")
            self.docs[doc_id] = deepcopy(doc)

        @staticmethod
        def _matches(doc, query):
            return all(doc.get(key) == value for key, value in query.items())

        @staticmethod
        def _project(doc, projection):
            if projection and any(value == 1 for value in projection.values()):
                projected = {key: deepcopy(doc[key]) for key, value in projection.items() if value == 1 and key in doc}
                if projection.get("_id") != 0 and "_id" in doc:
                    projected.setdefault("_id", deepcopy(doc["_id"]))
            else:
                projected = deepcopy(doc)
                if projection and projection.get("_id") == 0:
                    projected.pop("_id", None)
            return projected

    class FakeDatabase:
        def __init__(self, collections):
            self.collections = collections

        def __getitem__(self, collection_name):
            return FakeCollection(self.collections.setdefault(collection_name, {}))

    class FakeMongoClient:
        def __init__(self, uri, serverSelectionTimeoutMS=5000):
            metrics["client_count"] += 1
            self.uri = uri
            self.server_selection_timeout_ms = serverSelectionTimeoutMS

        def __getitem__(self, database_name):
            return FakeDatabase(store.setdefault(database_name, {}))

        def close(self):
            pass

    module = types.ModuleType("pymongo")
    module.MongoClient = FakeMongoClient
    module.metrics = metrics
    monkeypatch.setitem(sys.modules, "pymongo", module)
    return store


def set_shared_v4_mongo_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_DOMAIN_COLLECTION", "agent_v4_domain_items")
    monkeypatch.setenv("MONGODB_TABLE_CATALOG_COLLECTION", "agent_v4_table_catalog_items")
    monkeypatch.setenv("MONGODB_MAIN_FLOW_FILTER_COLLECTION", "agent_v4_main_flow_filters")
    monkeypatch.setenv("MONGODB_RESULT_COLLECTION", "agent_v4_result_store")


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _component_outputs(module):
    component_classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and value.__module__ == module.__name__ and hasattr(value, "outputs")
    ]
    assert len(component_classes) == 1
    return component_classes[0].outputs


def _component_inputs(module):
    component_classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and value.__module__ == module.__name__ and hasattr(value, "inputs")
    ]
    assert len(component_classes) == 1
    return component_classes[0].inputs


def test_langflow_components_do_not_import_project_helpers():
    forbidden = {"reference_runtime", "langflow_components", "utils", "helpers"}
    assert COMPONENT_FILES
    for path in COMPONENT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name} uses relative import"
                if node.module:
                    assert node.module.split(".")[0] not in forbidden, path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, path.name


def test_langflow_components_use_direct_lfx_imports_without_fallback_stubs():
    for path in COMPONENT_FILES:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        assert "from lfx.custom.custom_component.component import Component" in text
        assert "try:\n    from lfx" not in text, f"{path.name} has an lfx import fallback"
        local_classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        assert "Component" not in local_classes, f"{path.name} defines a local Component fallback"
        assert "DataInput" not in local_classes, f"{path.name} defines a local DataInput fallback"
        assert "Output" not in local_classes, f"{path.name} defines a local Output fallback"


def test_langflow_components_load_as_standalone_files():
    for path in COMPONENT_FILES:
        load_module(path)


def test_langflow_components_do_not_overlap_input_and_output_names():
    for path in COMPONENT_FILES:
        module = load_module(path)
        component_classes = [
            value
            for value in vars(module).values()
            if isinstance(value, type) and value.__module__ == module.__name__ and hasattr(value, "inputs") and hasattr(value, "outputs")
        ]
        for component_class in component_classes:
            input_names = {item.kwargs.get("name") for item in getattr(component_class, "inputs", []) if hasattr(item, "kwargs")}
            output_names = {item.kwargs.get("name") for item in getattr(component_class, "outputs", []) if hasattr(item, "kwargs")}
            assert not (input_names & output_names), f"{path.name} has overlapping input/output names: {input_names & output_names}"


def test_langflow_component_visible_labels_are_korean_first():
    def has_korean(text: str) -> bool:
        return any("\uac00" <= char <= "\ud7a3" for char in text)

    for path in COMPONENT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in {"display_name", "description"}
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                    ):
                        assert has_korean(node.value.value), f"{path.name}:{node.lineno} visible label is not Korean-first"
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if (
                        keyword.arg == "display_name"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        assert has_korean(keyword.value.value), f"{path.name}:{node.lineno} port label is not Korean-first"


def test_data_retriever_langflow_pipeline_dummy_path():
    validator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "06_retrieval_job_validator.py")
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    merger = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "13_source_retrieval_merger.py")
    adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14_retrieval_payload_adapter.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "wip_today",
                    "source_alias": "wip_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260701"},
                    "filters": {"OPER_NAME": {"operator": "in", "values": ["D/A1"]}},
                }
            ]
        }
    }

    validated = validator.validate_retrieval_payload(payload)
    dummy_bundle = router.route_retrieval_jobs(validated, "dummy")
    dummy_result = dummy.retrieve_dummy_data(dummy_bundle)
    merged = merger.merge_source_retrieval_payloads(validated, dummy_result)
    adapted = adapter.build_retrieval_payload(merged)
    output_names = {item.kwargs.get("name") for item in adapter.RetrievalPayloadAdapter.outputs}

    assert {"D/A1", "D/A2", "W/B1", "W/B2"}.issubset({row["OPER_NAME"] for row in adapted["runtime_sources"]["wip_data"]})
    assert adapted["source_results"][0]["source_execution"]["used_dummy_data"] is True
    assert adapted["source_results"][0]["source_execution"]["filters_applied_in_retriever"] is False
    assert adapted["source_results"][0]["pandas_filters"] == {"OPER_NAME": {"operator": "in", "values": ["D/A1"]}}
    assert output_names == {"payload_out"}
    assert "final_safe_payload" not in output_names


def test_retrieval_router_sends_jobs_only_to_dummy_when_mode_is_dummy():
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    payload = {
        "request": {"retrieval_mode": "dummy"},
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "wip_today", "source_alias": "wip_data", "source_type": "oracle"},
                {"dataset_key": "target", "source_alias": "target_data", "source_type": "goodocs"},
            ]
        }
    }
    input_names = {item.kwargs.get("name") for item in router.RetrievalJobRouter.inputs}

    dummy = router.route_retrieval_jobs(payload, "dummy")
    oracle = router.route_retrieval_jobs(payload, "oracle")
    goodocs = router.route_retrieval_jobs(payload, "goodocs")

    assert input_names == {"payload"}
    assert len(dummy["retrieval_job_bundle"]["jobs"]) == 2
    assert dummy["retrieval_job_bundle"]["retrieval_mode"] == "dummy"
    assert oracle["retrieval_job_bundle"]["jobs"] == []
    assert goodocs["retrieval_job_bundle"]["jobs"] == []


def test_retrieval_router_live_mode_routes_by_source_type():
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    payload = {
        "request": {"retrieval_mode": "live"},
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "wip_today", "source_alias": "wip_data", "source_type": "oracle"},
                {"dataset_key": "target", "source_alias": "target_data", "source_type": "goodocs"},
            ]
        }
    }

    dummy = router.route_retrieval_jobs(payload, "dummy")
    oracle = router.route_retrieval_jobs(payload, "oracle")
    goodocs = router.route_retrieval_jobs(payload, "goodocs")

    assert dummy["retrieval_job_bundle"]["jobs"] == []
    assert [job["dataset_key"] for job in oracle["retrieval_job_bundle"]["jobs"]] == ["wip_today"]
    assert [job["dataset_key"] for job in goodocs["retrieval_job_bundle"]["jobs"]] == ["target"]


def test_retrieval_merger_keeps_rows_once_and_uses_compact_trace_summary():
    merger = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "13_source_retrieval_merger.py")
    rows = [{"DEVICE": "DEV-A", "QTY": 7}]
    merged = merger.merge_source_retrieval_payloads(
        {"trace": {"warnings": [], "errors": [], "inspection": {}}},
        {
            "source_type": "goodocs",
            "status": "ok",
            "source_results": [
                {
                    "source_alias": "target_data",
                    "dataset_key": "target",
                    "source_type": "goodocs",
                    "status": "ok",
                    "row_count": 1,
                    "preview_rows": rows,
                    "rows": rows,
                    "data": rows,
                    "source_execution": {"used_dummy_data": False},
                    "errors": [],
                }
            ],
            "errors": [],
            "warnings": [],
        },
    )

    assert merged["_runtime_rows_by_alias"] == {"target_data": rows}
    assert "rows" not in merged["source_results"][0]
    assert "data" not in merged["source_results"][0]
    trace_source = merged["trace"]["inspection"]["data_retrieval"]["sources"][0]
    assert trace_source == {
        "source_alias": "target_data",
        "dataset_key": "target",
        "source_type": "goodocs",
        "status": "ok",
        "row_count": 1,
        "used_dummy_data": False,
        "error_count": 0,
    }


def test_retrieval_merger_preserves_job_validation_failure_status():
    merger = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "13_source_retrieval_merger.py")
    merged = merger.merge_source_retrieval_payloads(
        {
            "trace": {
                "warnings": [],
                "errors": [{"type": "missing_retrieval_job_field", "message": "source_alias is required"}],
                "inspection": {"data_retrieval": {"job_validation": {"error_count": 1}}},
            }
        }
    )

    retrieval = merged["trace"]["inspection"]["data_retrieval"]
    assert retrieval["job_validation"]["error_count"] == 1
    assert retrieval["status"] == "error"


def test_retrieval_execution_gate_blocks_required_source_failure_by_default():
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_data", "source_type": "oracle"}
            ]
        },
        "source_results": [
            {
                "dataset_key": "production",
                "source_alias": "production_data",
                "source_type": "oracle",
                "status": "error",
                "errors": [{"type": "timeout", "message": "Oracle timeout"}],
            }
        ],
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = gate.apply_retrieval_execution_gate(payload)

    assert result["execution_gate"]["status"] == "blocked"
    assert result["execution_gate"]["pandas_llm_allowed"] is False
    assert result["execution_gate"]["answer_llm_allowed"] is False
    assert result["analysis"]["status"] == "error"
    assert result["data"]["rows"] == []
    assert "production_data" in result["answer_message"]


def test_retrieval_execution_gate_continues_when_only_optional_source_fails():
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_data", "required": True},
                {"dataset_key": "uph", "source_alias": "uph_data", "required": False},
            ]
        },
        "source_results": [
            {"source_alias": "production_data", "status": "ok", "errors": []},
            {"source_alias": "uph_data", "status": "error", "errors": [{"type": "timeout", "message": "timeout"}]},
        ],
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = gate.apply_retrieval_execution_gate(payload)

    assert result["execution_gate"]["status"] == "continue"
    assert result["execution_gate"]["critical_failures"] == []
    assert result["execution_gate"]["optional_failures"][0]["source_alias"] == "uph_data"
    assert result["execution_gate"]["pandas_llm_allowed"] is True
    assert result["trace"]["warnings"][-1]["type"] == "optional_source_retrieval_failed"


def test_retrieval_guarded_agent_skips_model_only_when_gate_is_blocked():
    guarded = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14b_retrieval_guarded_agent.py")

    blocked_agent = guarded.RetrievalGuardedAgent()
    blocked_agent.control_payload = {"execution_gate": {"status": "blocked"}}
    blocked_message = asyncio.run(blocked_agent.message_response())

    success_agent = guarded.RetrievalGuardedAgent()
    success_agent.control_payload = {"execution_gate": {"status": "continue"}}
    success_agent._stub_response = "agent-success"
    success_message = asyncio.run(success_agent.message_response())

    assert blocked_message.text == ""
    assert "생략" in blocked_agent.status
    assert success_message.text == "agent-success"


def test_h_api_retriever_executes_configured_http_request():
    retriever = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "10_h_api_retriever.py")
    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"data":{"rows":[{"DEVICE":"D1","QTY":12}]}}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["method"] = request.get_method()
        return FakeResponse()

    payload = {
        "retrieval_job_bundle": {
            "jobs": [
                {
                    "dataset_key": "api_dataset",
                    "source_alias": "api_data",
                    "source_type": "h_api",
                    "source_config": {
                        "api_url": "https://example.test/items/{DATE}",
                        "method": "GET",
                        "response_path": "data.rows",
                    },
                    "required_params": {"DATE": "20260701", "PLANT": "PNT"},
                }
            ]
        }
    }

    result = retriever.h_api_retrieve(payload, api_token="token", timeout_seconds="7", opener=fake_open)
    source_result = result["source_results"][0]

    assert result["status"] == "ok"
    assert captured["method"] == "GET"
    assert captured["timeout"] == 7
    assert captured["url"].startswith("https://example.test/items/20260701?")
    assert source_result["rows"] == [{"DEVICE": "D1", "QTY": 12}]
    assert source_result["source_execution"]["used_dummy_data"] is False


def test_datalake_retriever_runs_lakehouse_style_client():
    retriever = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "11_datalake_retriever.py")
    calls = {}

    class FakeLakeHouse:
        def __init__(self, real_user_id=""):
            calls["real_user_id"] = real_user_id

        def ensure_running(self, cluster_type):
            calls["cluster_type"] = cluster_type

        def auto_run_sync_paragraph(self, code):
            calls["code"] = code

        def get_rst(self):
            return [{"DATE": "20260701", "QTY": 21}]

    payload = {
        "retrieval_job_bundle": {
            "jobs": [
                {
                    "dataset_key": "lake_dataset",
                    "source_alias": "lake_data",
                    "source_type": "datalake",
                    "source_config": {"query_template": "select * from t where work_date = {DATE}"},
                    "required_params": {"DATE": "20260701"},
                }
            ]
        }
    }

    result = retriever.datalake_retrieve(payload, user_id="u123", client_cls=FakeLakeHouse)
    source_result = result["source_results"][0]

    assert result["status"] == "ok"
    assert calls["real_user_id"] == "u123"
    assert calls["cluster_type"] == "starrocks"
    assert "work_date = '20260701'" in calls["code"]
    assert source_result["rows"] == [{"DATE": "20260701", "QTY": 21}]
    assert source_result["source_execution"]["adapter"] == "datalake"


def test_goodocs_retriever_uses_v3_goodocs_class_contract():
    retriever = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "12_goodocs_retriever.py")
    captured = {}

    class FakeGoodocs:
        def __init__(self, auth):
            captured["auth"] = auth

        def read_sheet(self, sheet_name):
            captured["sheet_name"] = sheet_name
            return [
                {"DEVICE": "D1", "TARGET": 100, "ROW_ID": "system"},
                {"DEVICE": "D2", "TARGET": 200, "LastUser": "system"},
            ]

    payload = {
        "retrieval_job_bundle": {
            "jobs": [
                {
                    "dataset_key": "target",
                    "source_alias": "target_data",
                    "source_type": "goodocs",
                    "source_config": {
                        "doc_id": "doc-1",
                        "sheet_name": "목표",
                    },
                }
            ]
        }
    }

    previous = retriever.GoodocsRetriever.goodocs_class
    retriever.GoodocsRetriever.goodocs_class = FakeGoodocs
    try:
        result = retriever.goodocs_retrieve(payload, user_id="user-1", token_source="token-source", token_key="token-key")
        source_result = result["source_results"][0]
    finally:
        retriever.GoodocsRetriever.goodocs_class = previous

    assert result["status"] == "ok"
    assert captured["auth"] == {
        "USER_ID": "user-1",
        "DOC_ID": "doc-1",
        "TOKEN_SOURCE": "token-source",
        "TOKEN_KEY": "token-key",
        "SHEET_NAME": "목표",
    }
    assert captured["sheet_name"] == "목표"
    assert source_result["rows"] == [{"DEVICE": "D1", "TARGET": 100}, {"DEVICE": "D2", "TARGET": 200}]
    assert source_result["source_execution"]["doc_id"] == "doc-1"
    assert source_result["source_execution"]["sheet_name"] == "목표"
    assert source_result["source_execution"]["used_dummy_data"] is False


def test_goodocs_retriever_keeps_inline_rows_for_local_fixture():
    retriever = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "12_goodocs_retriever.py")
    payload = {
        "retrieval_job_bundle": {
            "jobs": [
                {
                    "dataset_key": "target",
                    "source_alias": "target_data",
                    "source_type": "goodocs",
                    "source_config": {
                        "doc_id": "doc-1",
                        "sheet_name": "목표",
                        "rows": [{"DEVICE": "D1", "TARGET": 100, "ROW_ID": "system"}],
                    },
                }
            ]
        }
    }

    result = retriever.goodocs_retrieve(payload)
    source_result = result["source_results"][0]

    assert result["status"] == "ok"
    assert source_result["rows"] == [{"DEVICE": "D1", "TARGET": 100}]
    assert source_result["source_execution"]["source_configured"] is True
    assert "data" not in source_result


def test_goodocs_live_mode_never_falls_back_to_dummy_without_credentials():
    retriever = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "12_goodocs_retriever.py")
    payload = {
        "request": {"retrieval_mode": "live"},
        "retrieval_job_bundle": {
            "retrieval_mode": "live",
            "jobs": [
                {
                    "dataset_key": "target",
                    "source_alias": "target_data",
                    "source_type": "goodocs",
                    "source_config": {"doc_id": "doc-1"},
                }
            ],
        },
    }

    result = retriever.goodocs_retrieve(payload)
    source_result = result["source_results"][0]

    assert result["status"] == "error"
    assert source_result["status"] == "error"
    assert source_result["failure_type"] == "missing_goodocs_credentials"
    assert source_result["source_execution"]["used_dummy_data"] is False
    assert "data" not in source_result


def test_analysis_request_loader_defaults_reference_date_to_korea_today():
    request_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "00_analysis_request_loader.py")
    expected_today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")

    payload = request_loader.build_request("오늘 재공 알려줘")
    inherited = request_loader.build_request("오늘 재공 알려줘", {"session_id": "s-from-state"})
    input_names = {item.kwargs.get("name") for item in request_loader.AnalysisRequestLoader.inputs}

    assert payload["request"]["reference_date"] == expected_today
    assert payload["request"]["session_id"] == "demo-session"
    assert inherited["request"]["session_id"] == "s-from-state"
    assert "timezone" not in payload["request"]
    assert "reference_date_source" not in payload["request"]
    assert "reference_date" not in input_names
    assert "timezone" not in input_names
    assert "session_id" not in input_names


def test_session_state_flow_roundtrips_compact_state_in_shared_v4_collection(monkeypatch):
    loader = load_module(ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py")
    writer = load_module(ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py")
    store = install_fake_pymongo(monkeypatch)
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_SESSION_STATE_COLLECTION", "agent_v4_session_states")
    response_payload = {
        "request": {"session_id": "session-1", "question": "오늘 WB공정의 생산량 알려줘"},
        "state": {
            "runtime_sources": {"large": [{"drop": True}]},
            "last_question": "오늘 WB공정의 생산량 알려줘",
            "current_data": {
                "columns": ["DEVICE", "PRODUCTION"],
                "rows": [{"DEVICE": "DEV-A", "PRODUCTION": 10}, {"DEVICE": "DEV-B", "PRODUCTION": 20}],
                "row_count": 2,
                "data_ref": {"ref_id": "result:session-1:abc"},
                "source_aliases": ["production_data"],
                "source_dataset_keys": ["production_today"],
            },
            "last_intent_plan": {"analysis_kind": "production_sum"},
            "last_applied_criteria": {"metrics": ["PRODUCTION"]},
        },
    }

    written = writer.write_session_state(
        response_payload,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        session_collection_name="agent_v4_session_states",
        preview_row_limit="1",
    )
    doc = store["datagov"]["agent_v4_session_states"]["session_state:session-1"]
    loaded = loader.load_session_state(
        types.SimpleNamespace(text="어제 생산량은?", session_id="session-1"),
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        session_collection_name="agent_v4_session_states",
        preview_row_limit="1",
    )

    assert written["session_state_write"]["saved"] is True
    assert doc["session_id"] == "session-1"
    assert "runtime_sources" not in doc["state"]
    assert doc["state"]["current_data"]["rows"] == [{"DEVICE": "DEV-A", "PRODUCTION": 10}]
    assert doc["state"]["current_data"]["data_is_preview"] is True
    assert loaded["session_state_load"]["loaded"] is True
    assert loaded["session_state_load"]["collection_name"] == "agent_v4_session_states"
    assert loaded["state"]["session_id"] == "session-1"
    assert loaded["state"]["current_data"]["data_ref"]["ref_id"] == "result:session-1:abc"
    assert loaded["state"]["last_intent_plan"] == {"analysis_kind": "production_sum"}


def test_session_state_loader_returns_session_id_even_when_state_is_missing(monkeypatch):
    loader = load_module(ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py")
    install_fake_pymongo(monkeypatch)
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_SESSION_STATE_COLLECTION", "agent_v4_session_states")

    loaded = loader.load_session_state(
        types.SimpleNamespace(text="첫 질문", session_id="new-session"),
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        session_collection_name="agent_v4_session_states",
    )

    assert loaded["state"] == {"session_id": "new-session"}
    assert loaded["session_state_load"]["source"] == "mongodb_not_found"


def test_intent_variables_builder_hides_date_context_and_direct_specialized_prompt_ports():
    intent_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "02_intent_variables_builder.py")

    input_names = {item.kwargs.get("name") for item in intent_variables.IntentVariablesBuilder.inputs}
    output_names = {item.kwargs.get("name") for item in intent_variables.IntentVariablesBuilder.outputs}

    assert output_names == {"question", "state_summary", "metadata_candidates", "output_schema"}
    assert "reference_date" not in output_names
    assert "timezone" not in output_names
    assert "specialized_prompt" not in output_names
    assert "specialized_prompt_text" not in input_names


def test_intent_variables_builder_compacts_metadata_candidate_wrapper():
    intent_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "02_intent_variables_builder.py")
    variables = intent_variables.build_variables(
        {"request": {"question": "오늘 재공 알려줘", "reference_date": "20260701"}},
        {
            "domain_items": [{"key": "duplicated_outer"}],
            "metadata_candidates": {
                "domain_items": [{"section": "process_groups", "key": "DA"}],
                "table_catalog_items": [{"dataset_key": "wip_today"}],
            },
            "metadata_load": {"loads": {"domain_items": {"collection_name": "agent_v4_domain_items"}}},
        },
    )
    candidates = json.loads(variables["metadata_candidates"])
    schema = json.loads(variables["output_schema"])

    assert candidates == {
        "domain_items": [{"section": "process_groups", "key": "DA"}],
        "table_catalog_items": [{"dataset_key": "wip_today"}],
    }
    assert "metadata_candidates" not in candidates
    assert "metadata_load" not in candidates
    assert "pandas_function_case" not in schema["intent_plan"]
    assert schema["intent_plan"]["pandas_function_cases"] == []
    assert "request_scope" in schema["intent_plan"]
    assert "condition_resolution" in schema["intent_plan"]
    assert "\n" not in variables["state_summary"]
    assert "\n" not in variables["metadata_candidates"]
    assert "\n" not in variables["output_schema"]


def test_followup_hint_builder_detects_date_change_followup_without_pkg_fallback():
    hint_builder_path = ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    hint_builder = load_module(hint_builder_path)
    source_text = hint_builder_path.read_text(encoding="utf-8")
    payload = {
        "request": {"question": "어제 생산량은?", "reference_date": "20260707"},
        "state": {
            "last_question": "오늘 WB공정의 생산량 알려줘",
            "current_data": {
                "row_count": 1,
                "columns": ["PRODUCTION"],
                "source_aliases": ["production_data"],
                "source_dataset_keys": ["production_today"],
                "source_columns_by_alias": {"production_data": ["OPER_NAME", "DEVICE", "PRODUCTION"]},
                "data_ref": {"ref_id": "result:s1:abc"},
            },
            "last_intent_plan": {
                "analysis_kind": "production_sum",
                "retrieval_jobs": [
                    {
                        "dataset_key": "production_today",
                        "source_alias": "production_data",
                        "source_type": "oracle",
                        "required_params": {"DATE": "20260707"},
                        "filters": {"OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}},
                    }
                ],
                "pandas_execution_plan": [{"aggregate_column": "PRODUCTION"}],
            },
            "last_applied_criteria": {
                "required_params": {"production_data": {"DATE": "20260707"}},
                "analysis_filters": {"production_data": {"OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}}},
                "metrics": ["PRODUCTION"],
            },
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = hint_builder.build_followup_hint(payload)
    hint = result["followup_hint"]

    assert "W/B1" not in source_text
    assert "WB공정" not in source_text
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "followup_requery"
    assert hint["reuse_strategy_hint"] == "previous_intent_with_new_retrieval"
    assert hint["changed_conditions_hint"]["date"]["resolved_value"] == "20260706"
    assert "analysis_filters" in hint["inheritance_candidates"]
    previous_job = hint["previous_context"]["last_intent_plan"]["retrieval_jobs"][0]
    assert previous_job["filters"]["OPER_NAME"]["value"] == ["W/B1", "W/B2"]


def test_followup_hint_builder_keeps_complete_question_as_new_analysis():
    hint_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py")
    payload = {
        "request": {"question": "오늘 DA공정 생산량 알려줘", "reference_date": "20260707"},
        "state": {
            "last_question": "오늘 WB공정의 생산량 알려줘",
            "current_data": {"row_count": 1, "columns": ["PRODUCTION"], "data_ref": {"ref_id": "result:s1:abc"}},
        },
    }

    result = hint_builder.build_followup_hint(payload)
    hint = result["followup_hint"]

    assert hint["followup_candidate"] is False
    assert hint["request_scope_hint"] == "new_analysis"


def test_intent_variables_builder_includes_followup_hint_and_compact_previous_context():
    intent_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "02_intent_variables_builder.py")
    variables = intent_variables.build_variables(
        {
            "request": {"question": "어제 생산량은?", "reference_date": "20260707"},
            "followup_hint": {
                "followup_candidate": True,
                "request_scope_hint": "followup_requery",
                "reuse_strategy_hint": "previous_intent_with_new_retrieval",
            },
            "state": {
                "last_question": "오늘 WB공정의 생산량 알려줘",
                "last_answer_message": "이전 답변",
                "current_data": {
                    "row_count": 2,
                    "columns": ["PRODUCTION"],
                    "source_columns_by_alias": {"production_data": ["OPER_NAME", "DEVICE", "PRODUCTION"]},
                    "data_ref": {"ref_id": "result:s1:abc"},
                    "preview_rows": [{"PRODUCTION": 10}],
                    "raw_trace": {"large": "drop"},
                },
                "last_intent_plan": {"analysis_kind": "production_sum"},
                "last_applied_criteria": {"metrics": ["PRODUCTION"]},
            },
        }
    )

    state_summary = json.loads(variables["state_summary"])

    assert state_summary["followup_hint"]["request_scope_hint"] == "followup_requery"
    assert state_summary["state"]["last_question"] == "오늘 WB공정의 생산량 알려줘"
    assert state_summary["state"]["current_data"]["source_columns_by_alias"]["production_data"] == ["OPER_NAME", "DEVICE", "PRODUCTION"]
    assert "raw_trace" not in state_summary["state"]["current_data"]


def test_langflow_dummy_data_covers_data_catalog_shapes():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    expected_columns = {
        "production_today": {
            "WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1",
            "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY",
            "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "PRODUCTION",
        },
        "production": {
            "WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1",
            "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY",
            "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "PRODUCTION",
        },
        "wip_today": {
            "WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1",
            "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY",
            "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "WIP",
        },
        "wip": {
            "WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1",
            "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY",
            "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "WIP",
        },
        "target": {"DATE", "Mode", "DEN", "TECH", "PKG1", "PKG2", "LEAD", "ORG", "MCP NO", "INPUT 계획", "OUT 계획"},
        "equipment_assign": {
            "BAY_ID", "EQUIP_ID", "EQUIP_MODEL", "PRESS_CNT", "OPER", "OPER_NM", "MODE", "DENSITY",
            "TECH", "PKG1", "PKG2", "LEAD", "ORG", "PKGSIZE", "MCP_NO", "DEVICE", "DEVICE_DESC",
            "LOT_ID", "RECIPE_ID",
        },
        "eqp_uph": {
            "EQUIP_MODEL", "OPER", "OPER_NAME", "PRESS_CNT", "MODE", "TECH", "ORG", "DENSITY",
            "PKG1", "PKG2", "LEAD", "MCP_NO", "RECIPE_ID", "UPH", "LOAD_DT", "BASE_DT",
        },
        "lot_status": {
            "ERM_ID", "OPER", "OPER_NAME", "FAB", "OWNER", "GRADE", "DEVICE", "LOT_ID", "SUB_LOT_ID",
            "PROD_QTY", "WF_QTY", "IN_TAT", "CUM_TAT", "EQP_ID", "FLOW_ID", "OPER_IN_TM",
            "FAC_IN_TIME", "HOLD_STAT", "HOLD_REASON", "FAMILY", "MODE", "DENSITY", "TECH", "ORG",
            "PKG1", "PKG2", "PKG3", "LEAD", "MCP_NO", "THK_CD", "LOT_STAT", "LOT_GRP", "PKG_SIZE",
            "HOT_LOT", "HOT_LEVEL", "PKG_COMPOSIT", "DURABLE_ID", "DURABLE_TYP", "SUB_QTY",
            "TSV_DIE_TYPE", "EVENT_DESC", "MOVE_IN_TM", "PAD_ABNORMAL", "SWR_REQ_NO", "INSP_TARGET",
        },
        "hold_history": {
            "LOT_ID", "PROD_QTY", "OPER", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_USER", "HOLD_DESC",
            "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO",
            "GRADE", "OWNER", "DEVICE", "DEVICE_DESC", "PKG_SIZE", "THK_CD", "flow_id",
        },
    }
    jobs = [
        {
            "dataset_key": dataset_key,
            "source_alias": dataset_key,
            "source_type": "dummy",
            "required_params": _dummy_shape_params(dataset_key),
        }
        for dataset_key in expected_columns
    ]

    payload = dummy.retrieve_dummy_data({"retrieval_job_bundle": {"source_type": "dummy", "jobs": jobs}})
    results = {item["dataset_key"]: item for item in payload["source_results"]}

    assert set(results) == set(expected_columns)
    for dataset_key, columns in expected_columns.items():
        assert results[dataset_key]["row_count"] > 0
        assert columns.issubset(set(results[dataset_key]["columns"]))


def _dummy_shape_params(dataset_key):
    if dataset_key == "hold_history":
        return {"LOT_ID": "T1234567GEN1"}
    if dataset_key in {"production", "wip"}:
        return {"DATE": "20260630"}
    return {"DATE": "20260701"}


def test_langflow_dummy_data_applies_required_params_and_preserves_pandas_filters():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {
                        "dataset_key": "production_today",
                        "source_alias": "production_data",
                        "source_type": "dummy",
                        "required_params": {"DATE": "20260701"},
                        "filters": {"PKG_TYPE1": {"operator": "eq", "value": "LFBGA"}},
                    },
                    {
                        "dataset_key": "hold_history",
                        "source_alias": "hold_data",
                        "source_type": "dummy",
                        "required_params": {"LOT_ID": "T1234567GEN1"},
                    },
                ],
            }
        }
    )

    production, hold = payload["source_results"]

    assert {row["WORK_DATE"] for row in production["rows"]} == {"20260701"}
    assert {"LFBGA", "HBM", "UFBGA"}.issubset({row["PKG1"] for row in production["rows"]})
    assert production["pandas_filters"] == {"PKG_TYPE1": {"operator": "eq", "value": "LFBGA"}}
    assert production["source_execution"]["filters_applied_in_retriever"] is False
    assert {row["LOT_ID"] for row in hold["rows"]} == {"T1234567GEN1"}


def test_langflow_dummy_data_covers_auto_korea_today_reference_date():
    request_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "00_analysis_request_loader.py")
    validator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "06_retrieval_job_validator.py")
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")

    request_payload = request_loader.build_request("오늘 생산량 알려줘")
    reference_date = request_payload["request"]["reference_date"]
    payload = {
        "request": {**request_payload["request"], "retrieval_mode": "dummy"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": reference_date},
                }
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    validated = validator.validate_retrieval_payload(payload)
    routed = router.route_retrieval_jobs(validated, "dummy")
    retrieved = dummy.retrieve_dummy_data(routed)

    assert routed["retrieval_job_bundle"]["live_source_retrieval"] is False
    assert retrieved["status"] == "ok"
    assert retrieved["source_results"][0]["row_count"] > 0
    assert len(retrieved["source_results"][0]["preview_rows"]) <= 5
    assert len(retrieved["source_results"][0]["rows"]) > len(retrieved["source_results"][0]["preview_rows"])
    assert {row["WORK_DATE"] for row in retrieved["source_results"][0]["rows"]} == {reference_date}


def test_representative_questions_have_answerable_dummy_data_coverage():
    validator = load_module(ROOT / "tools" / "validate_representative_questions.py")
    modules = validator.load_flow_modules()
    results = {
        int(case["id"]): validator.run_case(case, modules, "20260701")
        for case in validator.representative_cases()
    }

    da_steps = {row["OPER_NAME"] for row in results[2]["preview_rows"]}
    wb_steps = {row["OPER_NAME"] for row in results[5]["preview_rows"]}
    hbm_wb_devices = {row["DEVICE"] for row in results[4]["preview_rows"]}
    hbm_fcb_devices = {row["DEVICE"] for row in results[6]["preview_rows"]}

    assert all(result["status"] == "ok" for result in results.values())
    assert all(result["data_mode"] == "dummy" for result in results.values())
    assert all("더미 데이터" in result["message"] for result in results.values())
    assert results[2]["row_count"] == 6
    assert da_steps == {"D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"}
    assert results[5]["row_count"] == 6
    assert wb_steps == {"W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"}
    assert results[4]["row_count"] == 2
    assert hbm_wb_devices == {"DEV-HBM", "DEV-HBM-B"}
    assert results[6]["row_count"] == 2
    assert hbm_fcb_devices == {"DEV-HBM", "DEV-HBM-B"}
    assert results[1]["preview_rows"][0]["MCP_NO"].startswith("L-267")
    assert results[8]["preview_rows"][0]["DEVICE"] == "DEV-RG-DDR4"
    assert results[9]["preview_rows"][0]["DEVICE"] == "DEV-SP-DDR5"
    assert results[12]["preview_rows"][0]["MCP_NO"] == "L-218K8H"
    assert results[13]["preview_rows"][0]["DEVICE"] == "DEV-DA-GDDR6"


def test_data_analysis_langflow_dummy_path_reaches_api_response():
    request_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "00_analysis_request_loader.py")
    intent_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "02_intent_variables_builder.py")
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    validator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "06_retrieval_job_validator.py")
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    merger = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "13_source_retrieval_merger.py")
    adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14_retrieval_payload_adapter.py")
    pandas_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py")
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    answer_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "18_answer_variables_builder.py")
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")

    expected_today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    payload = request_loader.build_request("오늘 D/A1 공정 WIP 합계 알려줘")
    intent_prompt_vars = intent_variables.build_variables(payload, {"datasets": ["wip_today"]})
    assert "wip_today" in intent_prompt_vars["metadata_candidates"]
    state_summary = json.loads(intent_prompt_vars["state_summary"])
    assert state_summary["request_context"]["reference_date"] == expected_today
    assert "timezone" not in state_summary["request_context"]
    assert "reference_date_source" not in state_summary["request_context"]
    assert "reference_date" not in intent_prompt_vars
    assert "timezone" not in intent_prompt_vars
    intent_llm_response = {
        "intent_plan": {
            "analysis_kind": "wip_sum_by_oper",
            "retrieval_jobs": [
                {
                    "dataset_key": "wip_today",
                    "source_alias": "wip_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260701"},
                    "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
                }
            ],
            "pandas_execution_plan": [{"step": "sum_wip", "source_alias": "wip_data", "group_by": ["OPER_NAME"]}],
            "output_contract": {"columns": ["OPER_NAME", "wip_sum"]},
        },
        "metadata_refs": [{"type": "table_catalog", "key": "wip_today"}],
        "trace": {"decision_reason": ["사용자가 WIP 합계를 요청했고 wip_today dataset을 사용한다."]},
    }
    payload = intent_normalizer.normalize_intent_plan(payload, intent_llm_response)

    validated = validator.validate_retrieval_payload(payload)
    dummy_bundle = router.route_retrieval_jobs(validated, "dummy")
    dummy_result = dummy.retrieve_dummy_data(dummy_bundle)
    merged = merger.merge_source_retrieval_payloads(validated, dummy_result)
    payload = adapter.build_retrieval_payload(merged)
    assert payload["source_results"][0]["row_count"] > 4
    assert payload["source_results"][0]["pandas_filters"] == {"OPER_NAME": {"operator": "eq", "value": "D/A1"}}

    pandas_prompt_vars = pandas_variables.build_variables(payload)
    assert "wip_data" in pandas_prompt_vars["source_schema_json"]
    pandas_llm_response = {
        "code": (
            "df = sources['wip_data']\n"
            "result = df.groupby('OPER_NAME', as_index=False)['WIP'].sum().rename(columns={'WIP': 'wip_sum'})"
        )
    }
    payload = pandas_executor.execute_pandas_code(payload, pandas_llm_response)

    assert payload["analysis"]["status"] == "ok"
    assert payload["data"]["rows"] == [{"OPER_NAME": "D/A1", "wip_sum": 363}]
    generated_code = payload["trace"]["inspection"]["pandas_execution"]["generated_code"]
    assert "OPER_NAME" in generated_code
    assert "_filter_values_1_1 = ['D/A1']" in generated_code
    assert ".isin(_filter_values_1_1)" in generated_code
    assert "df = sources['wip_data']" in generated_code
    assert payload["trace"]["inspection"]["pandas_execution"]["pandas_filter_plan"][0]["conditions"][0]["field"] == "OPER_NAME"

    answer_prompt_vars = answer_variables.build_variables(payload)
    assert "wip_sum" in answer_prompt_vars["result_summary_json"]
    payload = answer_builder.build_answer_response(payload, "D/A1 공정의 WIP 합계는 120입니다.")
    playground_message = message_adapter.build_message(payload, "", True)
    response = api_builder.build_api_response(payload)

    assert response["status"] == "ok"
    assert response["message"] == "분석 결과 OPER_NAME=D/A1, wip_sum=363입니다."
    assert response["answer_sections"]["summary"]["headline"] == response["message"]
    assert response["trace"]["inspection"]["answer_grounding"]["unsupported_numeric_claims"] == ["120"]
    assert response["data"]["row_count"] == 1
    assert response["intent_plan"]["pandas_execution_plan"][0]["step"] == "sum_wip"
    assert "analysis_code" not in response["analysis"]
    assert "rows" not in response["analysis"]
    assert response["trace"]["inspection"]["pandas_execution"]["generated_code"]
    assert "runtime_sources" not in response
    assert "_full_result_rows" not in response
    assert "_runtime_result_rows" not in response
    assert "### 의도 분석" in playground_message
    assert "wip_sum_by_oper" in playground_message
    assert "### 데이터 조회" in playground_message
    assert "wip_data" in playground_message
    assert "pandas 필터" in playground_message
    assert "### pandas 코드/실행" in playground_message
    assert "df = sources['wip_data']" in playground_message
    assert "| OPER_NAME | wip_sum |" in playground_message


def test_answer_message_adapter_result_table_uses_ten_row_preview():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "완료했습니다.",
        "data": {
            "columns": ["idx"],
            "rows": [{"idx": index} for index in range(12)],
            "row_count": 12,
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload, "", False, True, True)

    assert "| 9 |" in message
    assert "| 10 |" not in message
    assert "총 12건 중 10건을 표시했습니다." in message


def test_answer_message_adapter_exposes_repair_attempt_and_failure_reason():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "analysis": {
            "status": "error",
            "row_count": 0,
            "columns": [],
            "error": {"type": "unsafe_code", "message": "import 문은 허용하지 않습니다."},
        },
        "trace": {
            "inspection": {
                "pandas_execution": {
                    "status": "error",
                    "generated_code": "import os\nresult = sources['data']",
                    "error": {"type": "unsafe_code", "message": "import 문은 허용하지 않습니다."},
                },
                "pandas_repair": {
                    "attempted": True,
                    "llm_called": False,
                    "selected": "initial",
                    "reason": "repair LLM 호출이 실패해 초기 오류 결과를 유지했습니다.",
                    "initial_error": {"type": "unsafe_code", "message": "import 문은 허용하지 않습니다."},
                    "repair_error": {"type": "repair_llm_error", "message": "credential missing"},
                },
            }
        },
    }

    section = message_adapter._pandas_section(payload)

    assert "Repair 상태" in section
    assert "시도" in section and "예" in section
    assert "LLM 호출" in section and "아니오" in section
    assert "선택 결과" in section and "initial" in section
    assert "최초 오류" in section and "unsafe_code" in section
    assert "repair_llm_error" in section and "credential missing" in section


def test_answer_message_adapter_formats_numbers_and_shows_recorded_outputs():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "현재 재공 기준 분석 결과입니다.",
        "analysis": {
            "status": "ok",
            "step_outputs": [
                {
                    "key": "top_wip_product",
                    "description": "현재 재공이 가장 많은 제품",
                    "row_count": 1,
                    "columns": ["DEVICE", "WIP"],
                    "preview_rows": [{"DEVICE": "DEV-A", "WIP": 12000}],
                }
            ],
            "function_case_results": [
                {
                    "function_name": "sample_helper",
                    "input_text": "DEV-A",
                    "description": "특화 함수 결과",
                    "matched_count": 12,
                    "columns": ["DEVICE", "WIP"],
                    "preview_rows": [{"DEVICE": "DEV-A", "WIP": 12000}],
                }
            ],
        },
        "data": {
            "columns": ["DEVICE", "WIP", "ASSIGN_COUNT"],
            "rows": [{"DEVICE": "DEV-A", "WIP": 12000, "ASSIGN_COUNT": 9850}],
            "row_count": 1,
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload, "", False, True, True)

    assert "### 중간 분석 산출물" in message
    assert "### helper 실행 결과" in message
    assert "12K" in message
    assert "9,850" in message


def test_answer_message_adapter_compacts_product_token_match_preview():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "analysis": {
            "status": "ok",
            "function_case_results": [
                {
                    "function_name": "match_product_tokens",
                    "input_text": "RG 8G DDR4 x16 96 FCBGA SDP",
                    "description": "제품 속성 token 매칭 결과",
                    "matched_count": 12,
                    "columns": [
                        "WORK_DATE",
                        "SHIFT",
                        "FACTORY",
                        "FAB",
                        "TECH",
                        "DENSITY",
                        "MODE",
                        "ORG",
                        "PKG1",
                        "PKG2",
                        "LEAD",
                        "MCP_NO",
                        "DEVICE",
                        "DEVICE_DESC",
                        "WIP",
                    ],
                    "preview_rows": [
                        {
                            "WORK_DATE": "20260705",
                            "SHIFT": "1",
                            "FACTORY": "PNT",
                            "FAB": "PKG",
                            "TECH": "RG",
                            "DENSITY": "8G",
                            "MODE": "DDR4",
                            "ORG": "16",
                            "PKG1": "FCBGA",
                            "PKG2": "SDP",
                            "LEAD": "96",
                            "MCP_NO": "L-218K8H",
                            "DEVICE": "RG-X16",
                            "DEVICE_DESC": "RG 8G DDR4 X16 96 FCBGA SDP",
                            "WIP": 10,
                        }
                    ],
                }
            ],
        },
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload, "", False, True, True)

    assert "**제품 속성 token 매칭 결과**" in message
    assert "- 입력: `RG 8G DDR4 x16 96 FCBGA SDP`" in message
    assert "- 전체 매칭: `12`건" in message
    assert "- 미리보기: `1`건 표시" in message
    assert "\n\n| TECH | DENSITY | MODE | ORG | PKG1 | PKG2 | LEAD | MCP_NO | DEVICE | DEVICE_DESC | WIP |" in message
    assert "| TECH | DENSITY | MODE | ORG | PKG1 | PKG2 | LEAD | MCP_NO | DEVICE | DEVICE_DESC | WIP |" in message
    assert "WORK_DATE" not in message
    assert "SHIFT" not in message
    assert "FACTORY" not in message


def test_answer_message_adapter_splits_long_plain_answer_into_paragraphs():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    long_answer = (
        "오늘 DA 공정에서 제품별로 총 4개 제품의 생산량이 집계되었습니다. "
        "가장 많은 생산량을 보인 제품은 DEV002로 1,785개 생산되었습니다. "
        "이 외 DEV001은 1,341개, DEV003은 455개, DEV004는 307개 생산되었습니다. "
        "이는 DA 공정 생산량 데이터를 기준으로 분석한 결과입니다."
    )
    payload = {
        "answer_message": long_answer,
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload)

    assert "집계되었습니다.\n\n가장 많은 생산량" in message
    assert "생산되었습니다.\n\n이 외" in message
    assert "생산되었습니다.\n\n이는 DA" in message


def test_answer_message_adapter_uses_explicit_column_labels_only():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    module_text = (ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py").read_text(encoding="utf-8")

    assert "SERVICE_COLUMN_LABELS" not in module_text
    assert "_product_identity_columns" not in module_text
    assert '"TOTAL_PRODUCTION":' not in module_text

    payload = {
        "answer_sections": {
            "summary": {"headline": "표시명 테스트입니다."},
            "result_table": {
                "columns": ["RAW_DIM", "RAW_VALUE"],
                "display_columns": ["RAW_VALUE", "RAW_DIM"],
                "column_labels": {"RAW_DIM": "분류", "RAW_VALUE": "값"},
                "rows": [{"RAW_DIM": "A", "RAW_VALUE": 12000}],
                "display_rows": [{"RAW_DIM": "A", "RAW_VALUE": "12K"}],
                "row_count": 1,
            },
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload)

    assert "| 값 | 분류 |" in message
    assert "| 12K | A |" in message


def test_data_analysis_answer_response_builds_sections_for_api_and_message():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "request": {"question": "현재 재공이 가장 많은 제품 알려줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "wip_today",
                    "source_alias": "wip_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260705"},
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}},
                }
            ],
            "pandas_execution_plan": [{"groupby_columns": ["DEVICE"], "aggregate_column": "WIP"}],
        },
        "source_results": [
            {
                "dataset_key": "wip_today",
                "source_alias": "wip_data",
                "source_type": "oracle",
                "row_count": 3,
                "applied_params": {"DATE": "20260705"},
                "pandas_filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}},
            }
        ],
        "analysis": {
            "status": "ok",
            "step_outputs": [
                {
                    "key": "top_wip_product",
                    "description": "현재 재공이 가장 많은 제품",
                    "row_count": 1,
                    "columns": ["DEVICE", "WIP"],
                    "preview_rows": [{"DEVICE": "DEV-A", "WIP": 12500}],
                }
            ],
        },
        "data": {
            "columns": ["DEVICE", "WIP"],
            "rows": [{"DEVICE": "DEV-A", "WIP": 12500}],
            "row_count": 1,
        },
        "trace": {"warnings": [], "errors": [], "inspection": {"pandas_execution": {"generated_code": "result = df"}}},
    }

    payload = answer_builder.build_answer_response(payload, "현재 재공이 가장 많은 제품은 DEV-A이고, 재공수량은 12.5K입니다.")
    message = message_adapter.build_message(payload)
    diagnostic_message = message_adapter.build_message(payload, "", True)
    api_response = api_builder.build_api_response(payload, message)

    assert payload["answer_sections"]["result_table"]["row_source"] == "data.rows"
    assert "display_rows" not in payload["answer_sections"]["result_table"]
    assert "rows" not in payload["answer_sections"]["result_table"]
    assert payload["data"]["rows"][0]["WIP"] == 12500
    assert payload["answer_sections"]["applied_criteria"]["required_params"]["wip_data"] == {"DATE": "20260705"}
    assert "### 적용 기준" in message
    assert "**사용 데이터**" in message
    assert "- dataset_key=wip_today, source_alias=wip_data, source_type=oracle" in message
    assert "**조회 필수 조건**" in message
    assert "- wip_data: DATE=20260705" in message
    assert "**분석 조건**" in message
    assert "- wip_data: OPER_NAME={\"operator\": \"in\", \"value\": [\"D/A1\", \"D/A2\"]}" in message
    assert '- 사용 데이터: `[{"' not in message
    assert "### pandas 코드/실행" not in message
    assert "### pandas 코드/실행" in diagnostic_message
    assert api_response["answer_sections"]["result_table"]["row_count"] == 1
    assert "rows" not in api_response["answer_sections"]["result_table"]
    assert api_response["data"]["rows"][0]["WIP"] == 12500


def test_answer_response_builder_persists_compact_state_for_followup_turns():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    payload = {
        "request": {"question": "오늘 WB공정의 생산량 알려줘", "session_id": "s1", "reference_date": "20260707"},
        "intent_plan": {
            "analysis_kind": "production_sum",
            "request_scope": "new_analysis",
            "reuse_strategy": "none",
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260707"},
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}},
                }
            ],
            "pandas_execution_plan": [{"aggregate_column": "PRODUCTION"}],
        },
        "source_results": [
            {
                "dataset_key": "production_today",
                "source_alias": "production_data",
                "source_type": "oracle",
                "row_count": 2,
                "columns": ["OPER_NAME", "DEVICE", "PRODUCTION"],
                "applied_params": {"DATE": "20260707"},
                "pandas_filters": {"OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}},
            }
        ],
        "runtime_sources": {
            "production_data": [
                {"OPER_NAME": "W/B1", "DEVICE": "DEV-A", "PRODUCTION": 10},
                {"OPER_NAME": "W/B2", "DEVICE": "DEV-B", "PRODUCTION": 20},
            ]
        },
        "data": {
            "columns": ["PRODUCTION"],
            "rows": [{"PRODUCTION": 30}],
            "row_count": 1,
            "data_ref": {"ref_id": "result:s1:abc", "role": "analysis_result"},
        },
        "data_refs": [
            {"ref_id": "result:s1:abc", "role": "analysis_result"},
            {"ref_id": "result:s1:abc", "role": "source_rows", "source_alias": "production_data"},
        ],
        "analysis": {"status": "ok"},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = answer_builder.build_answer_response(payload, "오늘 WB공정 생산량은 30입니다.")
    state = result["state"]
    current_data = state["current_data"]

    assert state["last_question"] == "오늘 WB공정의 생산량 알려줘"
    assert state["last_answer_message"] == "오늘 WB공정 생산량은 30입니다."
    assert current_data["columns"] == ["PRODUCTION"]
    assert current_data["source_aliases"] == ["production_data"]
    assert current_data["source_dataset_keys"] == ["production_today"]
    assert current_data["source_columns_by_alias"]["production_data"] == ["OPER_NAME", "DEVICE", "PRODUCTION"]
    assert current_data["data_ref"]["ref_id"] == "result:s1:abc"
    assert state["followup_source_results"][0]["columns"] == ["OPER_NAME", "DEVICE", "PRODUCTION"]
    assert state["runtime_source_refs"]["production_data"]["role"] == "source_rows"
    assert state["last_intent_plan"]["retrieval_jobs"][0]["filters"]["OPER_NAME"]["value"] == ["W/B1", "W/B2"]
    assert state["last_applied_criteria"]["required_params"]["production_data"] == {"DATE": "20260707"}
    assert "runtime_sources" not in state


def test_answer_response_accepts_19_special_guidance_display_metadata():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "data": {
            "columns": ["OPER_NAME", "wip_sum"],
            "rows": [{"OPER_NAME": "D/A1", "wip_sum": 12500}],
            "row_count": 1,
        },
        "analysis": {"status": "ok"},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    llm_answer = {
        "answer_message": "D/A1 공정의 WIP 합계는 12.5K입니다.",
        "answer_sections": {
            "result_table": {
                "column_labels": {"OPER_NAME": "공정", "wip_sum": "WIP 합계"},
                "display_columns": ["wip_sum", "OPER_NAME"],
            }
        },
    }

    payload = answer_builder.build_answer_response(payload, llm_answer)
    message = message_adapter.build_message(payload)

    assert payload["answer_sections"]["result_table"]["column_labels"] == {"OPER_NAME": "공정", "wip_sum": "WIP 합계"}
    assert payload["answer_sections"]["result_table"]["display_columns"] == ["wip_sum", "OPER_NAME"]
    assert "| WIP 합계 | 공정 |" in message
    assert "| 12.5K | D/A1 |" in message


def test_answer_grounding_accepts_percentage_display_of_authoritative_value():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    payload = {
        "analysis": {"status": "ok"},
        "data": {"columns": ["달성률"], "rows": [{"달성률": 75}], "row_count": 1},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = answer_builder.build_answer_response(payload, "INPUT 계획 대비 달성률은 75%입니다.")

    assert result["answer_message"] == "INPUT 계획 대비 달성률은 75%입니다."
    assert "answer_grounding" not in result["trace"]["inspection"]


def test_intent_normalizer_parses_langflow_message_text_with_nested_json():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "오늘 da공정 생산량 상위 3개 제품 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = types.SimpleNamespace(
        text="""```json
{
  "intent_plan": {
    "analysis_kind": "top_product_production",
    "retrieval_jobs": [
      {
        "dataset_key": "production_today",
        "source_alias": "production_data",
        "source_type": "oracle",
        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT * FROM PROD_TABLE WHERE WORK_DATE = {DATE}"},
        "required_params": {"DATE": "20260701"},
        "filters": {"OPER_NAME": {"operator": "contains", "value": "D/A"}}
      }
    ],
    "pandas_execution_plan": [{"step": "top_n", "source_alias": "production_data"}],
    "output_contract": {"top_n": 3}
  },
  "metadata_refs": [{"type": "table_catalog", "key": "production_today"}],
  "trace": {"decision_reason": ["production_today를 선택"]}
}
```"""
    )

    normalized = intent_normalizer.normalize_intent_plan(payload, llm_response)

    assert normalized["intent_plan"]["retrieval_jobs"][0]["dataset_key"] == "production_today"
    assert normalized["intent_plan"]["retrieval_jobs"][0]["required_params"] == {"DATE": "20260701"}
    assert normalized["metadata_refs"] == [{"type": "table_catalog", "key": "production_today"}]
    assert normalized["trace"]["inspection"]["intent"]["retrieval_job_count"] == 1
    assert not any(warning.get("type") == "missing_retrieval_jobs" for warning in normalized["trace"]["warnings"])


def test_intent_normalizer_accepts_llm_json_with_literal_sql_newlines():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "어제 DA공정 차수별 생산량 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = types.SimpleNamespace(
        text="""{
  "intent_plan": {
    "analysis_kind": "data_retrieval_and_analysis",
    "retrieval_jobs": [
      {
        "dataset_key": "production",
        "source_alias": "production_data",
        "source_type": "oracle",
        "source_config": {
          "source_type": "oracle",
          "db_key": "PNT_RPT",
          "query_template": "SELECT *
FROM PROD_TABLE
WHERE WORK_DATE = {DATE}"
        },
        "required_params": {"DATE": "20260630"},
        "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}}
      }
    ],
    "pandas_execution_plan": [{"operation": "group_by", "source_alias": "production_data"}],
    "output_contract": {}
  }
}"""
    )

    normalized = intent_normalizer.normalize_intent_plan(payload, llm_response)

    assert normalized["intent_plan"]["analysis_kind"] == "data_retrieval_and_analysis"
    assert normalized["intent_plan"]["retrieval_jobs"][0]["source_config"]["query_template"].startswith("SELECT *")
    assert normalized["trace"]["inspection"]["intent"]["retrieval_job_count"] == 1


def test_intent_normalizer_preserves_arbitrary_analysis_kind_without_fallback():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    arbitrary_kind = "custom_metric_by_scope_v99"
    payload = {"request": {"question": "임의 분석 유형 보존"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = {
        "intent_plan": {
            "analysis_kind": arbitrary_kind,
            "retrieval_jobs": [{"dataset_key": "target", "source_alias": "target_data"}],
            "pandas_execution_plan": [{"step": "custom aggregation"}],
        }
    }

    normalized = intent_normalizer.normalize_intent_plan(payload, llm_response)

    assert normalized["intent_plan"]["analysis_kind"] == arbitrary_kind
    assert normalized["trace"]["inspection"]["intent"]["analysis_kind"] == arbitrary_kind


def test_intent_normalizer_recovers_intent_plan_when_metadata_refs_are_malformed():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "어제 DA공정 차수별 생산량 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = types.SimpleNamespace(
        text="""{
  "intent_plan": {
    "analysis_kind": "data_retrieval_and_analysis",
    "retrieval_jobs": [
      {
        "dataset_key": "production",
        "source_alias": "production_data",
        "source_type": "oracle",
        "required_params": {"DATE": "20260630"},
        "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}}
      }
    ],
    "pandas_execution_plan": [{"operation": "group_by", "source_alias": "production_data"}],
    "output_contract": {}
  },
  "metadata_refs": [
    {"section": "process_groups", "key": "DA"}],
    {"section": "analysis_recipes", "key": "group_by_oper_name_for_process_sequence"}
  ],
  "trace": {"decision_reason": ["metadata_refs 문법이 깨져도 intent_plan은 복구한다."]}
}"""
    )

    normalized = intent_normalizer.normalize_intent_plan(payload, llm_response)

    assert normalized["intent_plan"]["analysis_kind"] == "data_retrieval_and_analysis"
    assert normalized["intent_plan"]["retrieval_jobs"][0]["dataset_key"] == "production"
    assert normalized["trace"]["inspection"]["intent"]["retrieval_job_count"] == 1
    assert normalized["metadata_refs"] == []


def test_pandas_executor_parses_langflow_message_text_json():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {"runtime_sources": {"production_data": [{"MODE": "LPDDR5", "PRODUCTION": 1000}]}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = types.SimpleNamespace(text='```json\n{"code": "df = sources[\'production_data\']\\nresult = df"}\n```')

    result = pandas_executor.execute_pandas_code(payload, llm_response)

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["rows"] == [{"MODE": "LPDDR5", "PRODUCTION": 1000}]


def test_pandas_executor_accepts_llm_json_with_literal_code_newlines():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {"runtime_sources": {"production_data": [{"MODE": "LPDDR5", "PRODUCTION": 1000}]}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    llm_response = types.SimpleNamespace(
        text="""{
  "code": "df = sources['production_data']
result = df"
}"""
    )

    result = pandas_executor.execute_pandas_code(payload, llm_response)

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["rows"] == [{"MODE": "LPDDR5", "PRODUCTION": 1000}]


def test_pandas_executor_prepends_non_required_filters_before_aggregation():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260701"},
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]}},
                }
            ],
            "pandas_execution_plan": [{"step": "top_3_products"}],
        },
        "runtime_sources": {
            "production_data": [
                {"WORK_DATE": "20260701", "OPER_NAME": "D/A1", "TECH": "1Z", "DEN": "16G", "MODE": "LPDDR5", "PKG_TYPE1": "LFBGA", "PKG_TYPE2": "POP", "LEAD": "200", "MCP_NO": "M-001", "DEVICE": "DEV001", "PRODUCTION": 1000},
                {"WORK_DATE": "20260701", "OPER_NAME": "D/A2", "TECH": "1A", "DEN": "24G", "MODE": "HBM3E", "PKG_TYPE1": "HBM", "PKG_TYPE2": "TSV", "LEAD": "300", "MCP_NO": "H-001", "DEVICE": "DEV-HBM", "PRODUCTION": 700},
                {"WORK_DATE": "20260701", "OPER_NAME": "W/B1", "TECH": "1B", "DEN": "32G", "MODE": "LPDDR5X", "PKG_TYPE1": "UFBGA", "PKG_TYPE2": "MOBILE", "LEAD": "180", "MCP_NO": "M-002", "DEVICE": "DEV002", "PRODUCTION": 650},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    llm_response = {
        "code": (
            "grouped_data = sources[\"production_data\"].groupby([\"TECH\", \"DEN\", \"MODE\", \"PKG_TYPE1\", \"PKG_TYPE2\", \"LEAD\", \"MCP_NO\", \"DEVICE\"])[\"PRODUCTION\"].sum().reset_index()\n"
            "grouped_data = grouped_data.rename(columns={\"PRODUCTION\": \"TOTAL_PRODUCTION\"})\n"
            "sorted_data = grouped_data.sort_values(by=\"TOTAL_PRODUCTION\", ascending=False)\n"
            "result = sorted_data.head(3)"
        )
    }

    result = pandas_executor.execute_pandas_code(payload, llm_response)
    generated_code = result["trace"]["inspection"]["pandas_execution"]["generated_code"]

    assert result["analysis"]["status"] == "ok"
    assert [row["DEVICE"] for row in result["data"]["rows"]] == ["DEV001", "DEV-HBM"]
    assert "W/B1" not in json.dumps(result["data"]["rows"], ensure_ascii=False)
    assert "OPER_NAME" in generated_code
    assert "_filter_values_1_1 = ['D/A1', 'D/A2', 'D/A3', 'D/A4', 'D/A5', 'D/A6']" in generated_code
    assert ".isin(_filter_values_1_1)" in generated_code
    assert "grouped_data = sources[\"production_data\"].groupby" in generated_code


def test_pandas_executor_supports_prefix_filter_and_product_token_helper():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "required_params": {"DATE": "20260701"},
                    "filters": {"MCP_NO": {"operator": "starts_with", "value": "L-267"}},
                }
            ],
            "pandas_execution_plan": [],
        },
        "runtime_sources": {
            "production_data": [
                {"TECH": "1C", "DENSITY": "16G", "MODE": "LPDDR5", "LEAD": "267", "MCP_NO": "L-267A1", "DEVICE": "DEV-L267", "PRODUCTION": 10},
                {"TECH": "1Y", "DENSITY": "8G", "MODE": "LPDDR4", "LEAD": "218", "MCP_NO": "L-218K8H", "DEVICE": "DEV-L218K8H", "PRODUCTION": 20},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    result = pandas_executor.execute_pandas_code(payload, {"code": "result = sources['production_data'][['DEVICE', 'MCP_NO']]"})

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["rows"] == [{"DEVICE": "DEV-L267", "MCP_NO": "L-267A1"}]
    assert ".str.startswith(str(_filter_values_1_1[0]), na=False)" in result["trace"]["inspection"]["pandas_execution"]["generated_code"]

    helper_payload = {
        "runtime_sources": {
            "wip_data": [
                {"TECH": "DA", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180, "DEVICE": "DEV-DA-GDDR6", "WIP": 33},
                {"TECH": "DA", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180.0, "DEVICE": "DEV-DA-GDDR6-FLOAT", "WIP": 44},
                {"TECH": "ZZ", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180.0, "DEVICE": "DEV-ZZ-GDDR6", "WIP": 99},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    helper_result = pandas_executor.execute_pandas_code(
        helper_payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('DA 16G GDDR6 180', sources['wip_data'])\nresult = df[['TECH', 'DEVICE', 'WIP']]"},
    )

    assert helper_result["analysis"]["status"] == "ok"
    assert helper_result["data"]["rows"] == [
        {"TECH": "DA", "DEVICE": "DEV-DA-GDDR6", "WIP": 33},
        {"TECH": "DA", "DEVICE": "DEV-DA-GDDR6-FLOAT", "WIP": 44},
    ]
    helper_trace = helper_result["trace"]["inspection"]["pandas_execution"]
    effective_code = helper_trace["generated_code"]
    assert helper_trace["used_helpers"] == ["match_product_tokens"]
    assert helper_result["analysis"]["used_helpers"] == ["match_product_tokens"]
    assert "effective_code_with_helpers" not in helper_result["analysis"]
    assert "analysis_code" not in helper_result["analysis"]
    assert "def match_product_tokens" in effective_code
    assert "df = match_product_tokens('DA 16G GDDR6 180', sources['wip_data'])" in effective_code

    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    helper_message = message_adapter.build_message(helper_result, "", False, True, True)
    assert "### helper 실행 결과" in helper_message
    assert "제품 속성 token 매칭 결과" in helper_message
    assert "DA 16G GDDR6 180" in helper_message
    assert "def match_product_tokens" not in helper_message
    helper_diagnostic_message = message_adapter.build_message(helper_result, "", True)
    assert "사용 helper" in helper_diagnostic_message
    assert "생성된 pandas 코드" in helper_diagnostic_message
    assert "def match_product_tokens" in helper_diagnostic_message


def test_match_product_tokens_handles_org_x_lead_mcp_and_multiple_products():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "product_data": [
                {"TECH": "RG", "DENSITY": "8G", "MODE": "DDR4", "ORG": "16", "LEAD": "96", "PKG1": "FCBGA", "PKG2": "SDP", "MCP_NO": "L-218K8H", "DEVICE": "RG-X16", "WIP": 10},
                {"TECH": "CP", "DENSITY": "16G", "MODE": "DDR", "ORG": "8", "LEAD": "78", "PKG1": "FCBGA", "PKG2": "SDP", "MCP_NO": "L-216A1", "DEVICE": "CP-X8", "WIP": 20},
                {"TECH": "CP", "DENSITY": "16G", "MODE": "DDR", "ORG": "16", "LEAD": "78", "PKG1": "VFBGA", "PKG2": "SDP", "MCP_NO": "A-663Z9", "DEVICE": "CP-F78-V", "WIP": 30},
                {"TECH": "RG", "DENSITY": "8G", "MODE": "DDR4", "ORG": "16", "LEAD": "96", "PKG1": "VFBGA", "PKG2": "SDP", "MCP_NO": "A-777Z9", "DEVICE": "RG-F96-V", "WIP": 35},
                {"TECH": "RG", "DENSITY": "8G", "MODE": "DDR4", "ORG": "8", "LEAD": "96", "PKG1": "FCBGA", "PKG2": "SDP", "MCP_NO": "L-999", "DEVICE": "RG-WRONG-ORG", "WIP": 40},
                {"TECH": "CP", "DENSITY": "16G", "MODE": "DDR", "ORG": "8", "LEAD": "96", "PKG1": "FCBGA", "PKG2": "SDP", "MCP_NO": "L-000", "DEVICE": "CP-WRONG-LEAD", "WIP": 50},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    multi = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\n"
                "df = match_product_tokens('RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP', sources['product_data'])\n"
                "result = df[['DEVICE', 'ORG', 'LEAD']]"
            )
        },
    )
    fc78 = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('FC78', sources['product_data'])\nresult = df[['DEVICE', 'PKG1', 'LEAD']]"},
    )
    f78 = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('F78', sources['product_data'])\nresult = df[['DEVICE', 'PKG1', 'LEAD']]"},
    )
    fc96 = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('FC96', sources['product_data'])\nresult = df[['DEVICE', 'PKG1', 'LEAD']]"},
    )
    f96 = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('F96', sources['product_data'])\nresult = df[['DEVICE', 'PKG1', 'LEAD']]"},
    )
    mcp = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('L-218, L-216, A-663 제품 PKG 투입수량 알려줘', sources['product_data'])\nresult = df[['DEVICE', 'MCP_NO']]"},
    )

    assert multi["analysis"]["status"] == "ok"
    assert [row["DEVICE"] for row in multi["data"]["rows"]] == ["RG-X16", "CP-X8"]
    assert [row["DEVICE"] for row in fc78["data"]["rows"]] == ["CP-X8"]
    assert [row["DEVICE"] for row in f78["data"]["rows"]] == ["CP-X8", "CP-F78-V"]
    assert [row["DEVICE"] for row in fc96["data"]["rows"]] == ["RG-X16", "RG-WRONG-ORG", "CP-WRONG-LEAD"]
    assert [row["DEVICE"] for row in f96["data"]["rows"]] == ["RG-X16", "RG-F96-V", "RG-WRONG-ORG", "CP-WRONG-LEAD"]
    assert [row["MCP_NO"] for row in mcp["data"]["rows"]] == ["L-218K8H", "L-216A1", "A-663Z9"]


def test_match_product_tokens_scans_all_candidate_columns_without_preferred_role_lock():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "product_data": [
                {"TECH": "RG", "DENSITY": "8G", "MODE": "DDR4", "ORG": "8", "LEAD": "96", "DEVICE": "TECH-RG", "DEVICE_DESC": "plain", "WIP": 1},
                {"TECH": "XX", "DENSITY": "4G", "MODE": "SDR", "ORG": "4", "LEAD": "12", "DEVICE": "RG", "DEVICE_DESC": "RG SPECIAL", "WIP": 2},
                {"TECH": "ZZ", "DENSITY": "8G", "MODE": "SDR", "ORG": "16", "LEAD": "12", "DEVICE": "ONLY-16", "DEVICE_DESC": "group 16", "WIP": 3},
                {"TECH": "ZZ", "DENSITY": "16G", "MODE": "SDR", "ORG": "8", "LEAD": "12", "DEVICE": "DEN-16G", "DEVICE_DESC": "density product", "WIP": 4},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    rg_result = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('RG', sources['product_data'])\nresult = df[['DEVICE']]"},
    )
    density_result = pandas_executor.execute_pandas_code(
        payload,
        {"code": function_case_source("match_product_tokens") + "\n\ndf = match_product_tokens('16G', sources['product_data'])\nresult = df[['DEVICE']]"},
    )

    assert rg_result["analysis"]["status"] == "ok"
    assert [row["DEVICE"] for row in rg_result["data"]["rows"]] == ["TECH-RG", "RG"]
    assert [row["DEVICE"] for row in density_result["data"]["rows"]] == ["DEN-16G"]


def test_match_product_tokens_generalizes_special_pattern_rules():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "product_data": [
                {"PKG1": "FCBGA", "LEAD": "12", "ORG": "24", "MCP_NO": "L-999A1", "DEVICE": "FC12-X24-L999"},
                {"PKG1": "VFBGA", "LEAD": "12", "ORG": "24", "MCP_NO": "A-777Z1", "DEVICE": "F12-X24-A777"},
                {"PKG1": "FCBGA", "LEAD": "20", "ORG": "16", "MCP_NO": "L-200B1", "DEVICE": "FC20-X16-L200"},
                {"PKG1": "UFBGA", "LEAD": "344", "ORG": "8", "MCP_NO": "A-344C1", "DEVICE": "F344-UFBGA"},
                {"PKG1": "FCBGA", "LEAD": "344", "ORG": "24", "MCP_NO": "L-344D1", "DEVICE": "FC344-FCBGA"},
                {"PKG1": "BGA", "LEAD": "55", "ORG": "4", "MCP_NO": "B-123C1", "DEVICE": "B123-MCP"},
                {"PKG1": "BGA", "LEAD": "56", "ORG": "4", "MCP_NO": "Z-000D1", "DEVICE": "Z000-MCP"},
                {"PKG1": "BGA", "LEAD": "57", "ORG": "4", "MCP_NO": "Q-555A9", "DEVICE": "Q555-MCP"},
                {"PKG1": "BGA", "LEAD": "24", "ORG": "99", "MCP_NO": "N-024X1", "DEVICE": "LEAD24-NOT-X24"},
                {"TECH": "SP", "DENSITY": "16G", "MODE": "DDR5", "ORG": "4", "PKG1": "FCBGA", "PKG2": "SDP", "LEAD": "78", "MCP_NO": "", "DEVICE": "DEV-SP-DDR5-FCBGA78", "DEVICE_DESC": "SP 16G DDR5 2ND X4 78 FCBGA SDP"},
                {"TECH": "ZZ", "DENSITY": "99G", "MODE": "DDR5", "ORG": "4", "PKG1": "FCBGA", "PKG2": "SDP", "LEAD": "78", "MCP_NO": "S-111A1", "DEVICE": "MCP-PREFIX-CONTROL"},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    cases = {
        "FC12": ["FC12-X24-L999"],
        "FC20": ["FC20-X16-L200"],
        "F344": ["F344-UFBGA", "FC344-FCBGA"],
        "x24": ["FC12-X24-L999", "F12-X24-A777", "FC344-FCBGA"],
        "L-999": ["FC12-X24-L999"],
        "A-777": ["F12-X24-A777"],
        "B-123": ["B123-MCP"],
        "Z-000": ["Z000-MCP"],
        "Q-555": ["Q555-MCP"],
        "x99": ["LEAD24-NOT-X24"],
        "SP 16G DDR5 2ND X4 78 FCBGA SDP": ["DEV-SP-DDR5-FCBGA78"],
    }

    for query, expected_devices in cases.items():
        result = pandas_executor.execute_pandas_code(
            payload,
            {
                "code": (
                    function_case_source("match_product_tokens")
                    + f"\n\ndf = match_product_tokens({query!r}, sources['product_data'])\n"
                    + "result = df[['DEVICE']]"
                )
            },
        )

        assert result["analysis"]["status"] == "ok"
        assert [row["DEVICE"] for row in result["data"]["rows"]] == expected_devices

    desc_token_only = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('2ND', sources['product_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )
    unknown_token_only = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('UNKNOWN_TOKEN', sources['product_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )

    assert desc_token_only["analysis"]["status"] == "ok"
    assert desc_token_only["data"]["rows"] == [{"DEVICE": "DEV-SP-DDR5-FCBGA78"}]
    assert unknown_token_only["analysis"]["status"] == "ok"
    assert unknown_token_only["data"]["rows"] == []


def test_match_product_tokens_strips_lead_ball_suffix_only_for_lead_role():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "product_data": [
                {"TECH": "AA", "LEAD": "78ball", "MODE": "DDR5", "DEVICE": "LEAD-78-BALL"},
                {"TECH": "BB", "LEAD": "152Lead", "MODE": "LPDDR5", "DEVICE": "LEAD-152-LEAD"},
                {"TECH": "CC", "LEAD": 152, "MODE": "GDDR6", "DEVICE": "LEAD-152-NUMERIC"},
                {"TECH": "78LEAD", "LEAD": "999", "MODE": "SDR", "DEVICE": "TECH-78LEAD-CONTROL"},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    lead_78 = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('78Lead', sources['product_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )
    lead_152 = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('152ball', sources['product_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )

    assert lead_78["analysis"]["status"] == "ok"
    assert [row["DEVICE"] for row in lead_78["data"]["rows"]] == ["LEAD-78-BALL"]
    assert lead_152["analysis"]["status"] == "ok"
    assert [row["DEVICE"] for row in lead_152["data"]["rows"]] == ["LEAD-152-LEAD", "LEAD-152-NUMERIC"]


def test_match_product_tokens_requires_all_tokens_per_product_group():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "wip_data": [
                {"TECH": "1Z", "DENSITY": "16G", "MODE": "LPDDR5", "ORG": "PKG", "PKG1": "LFBGA", "PKG2": "POP", "LEAD": "200", "MCP_NO": "M-001", "DEVICE": "DEV001", "DEVICE_DESC": "LPDDR5 sample", "WIP": 128},
                {"TECH": "RG", "DENSITY": "32G", "MODE": "DDR4", "ORG": "DDP", "PKG1": "FBGA", "PKG2": "DDP", "LEAD": "96", "MCP_NO": "", "DEVICE": "DEV-RG-DDR4", "DEVICE_DESC": "RG 32G DDR4 FBGA 96 DDP product", "WIP": 77},
                {"TECH": "SP", "DENSITY": "16G", "MODE": "DDR5", "ORG": "4", "PKG1": "FCBGA", "PKG2": "SDP", "LEAD": "78", "MCP_NO": "", "DEVICE": "DEV-SP-DDR5", "DEVICE_DESC": "SP 16G DDR5 2ND X4 78 FCBGA SDP product", "WIP": 60},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    strict_no_partial = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP', sources['wip_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )
    desc_supported = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                function_case_source("match_product_tokens")
                + "\n\ndf = match_product_tokens('SP 16G DDR5 2ND X4 78 FCBGA SDP', sources['wip_data'])\n"
                + "result = df[['DEVICE']]"
            )
        },
    )

    assert strict_no_partial["analysis"]["status"] == "ok"
    assert strict_no_partial["data"]["rows"] == []
    assert desc_supported["analysis"]["status"] == "ok"
    assert desc_supported["data"]["rows"] == [{"DEVICE": "DEV-SP-DDR5"}]


def test_function_case_helper_record_fallback_is_standalone_and_executor_safe():
    import pandas as pd

    helper_code = function_case_source()
    namespace = {}
    exec(helper_code, namespace)
    standalone_result = namespace["match_product_tokens"](
        "DA 16G GDDR6 180",
        pd.DataFrame(
            [
                {"TECH": "DA", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180, "DEVICE": "DEV-DA"},
                {"TECH": "DA", "DENSITY": "8G", "MODE": "GDDR6", "LEAD": 180, "DEVICE": "DEV-OTHER"},
            ]
        ),
    )

    assert standalone_result["DEVICE"].tolist() == ["DEV-DA"]
    assert namespace["_function_case_results"][0]["function_name"] == "match_product_tokens"
    assert namespace["_function_case_results"][0]["matched_count"] == 1

    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    executor_result = pandas_executor.execute_pandas_code(
        {
            "runtime_sources": {
                "wip_data": [
                    {"TECH": "DA", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180, "DEVICE": "DEV-DA", "WIP": 33},
                    {"TECH": "ZZ", "DENSITY": "16G", "MODE": "GDDR6", "LEAD": 180, "DEVICE": "DEV-ZZ", "WIP": 99},
                ]
            },
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        {
            "code": (
                helper_code
                + "\n\n"
                "df = match_product_tokens('DA 16G GDDR6 180', sources['wip_data'])\n"
                "result = df[['DEVICE', 'WIP']]"
            )
        },
    )

    assert executor_result["analysis"]["status"] == "ok"
    assert executor_result["analysis"]["used_helpers"] == ["match_product_tokens"]
    function_case_results = executor_result["analysis"]["function_case_results"]
    assert function_case_results[0]["function_name"] == "match_product_tokens"
    assert function_case_results[0]["matched_count"] == 1


def test_answer_message_adapter_skips_duplicate_result_table_when_answer_has_table():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "요청 결과입니다.\n\n| OPER_NAME | wip_sum |\n| --- | ---: |\n| D/A1 | 363 |",
        "data": {
            "columns": ["OPER_NAME", "wip_sum"],
            "rows": [{"OPER_NAME": "D/A1", "wip_sum": 363}],
            "row_count": 1,
        },
        "intent_plan": {"analysis_kind": "wip_sum_by_oper"},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    message = message_adapter.build_message(payload, "", True)

    assert message.count("| OPER_NAME | wip_sum |") == 1
    assert "wip_sum_by_oper" in message


def test_answer_message_adapter_adds_data_ref_download_links():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "완료했습니다.",
        "data": {
            "columns": ["DEVICE", "QTY"],
            "rows": [{"DEVICE": "A", "QTY": 1}],
            "row_count": 1,
        },
        "data_refs": [
            {
                "store": "mongodb",
                "ref_id": "result:s1:abc",
                "database": "datagov",
                "collection_name": "agent_v4_result_store",
                "path": "payload.result_rows",
                "role": "analysis_result",
                "label": "분석 결과 데이터",
            },
            {
                "store": "mongodb",
                "ref_id": "result:s1:abc",
                "database": "datagov",
                "collection_name": "agent_v4_result_store",
                "path": "payload.runtime_sources.production_data",
                "role": "source_rows",
                "source_alias": "production_data",
                "label": "사용 원본 데이터: production_data",
            },
        ],
    }

    message = message_adapter.build_message(payload, "http://localhost:8501")
    input_names = {item.kwargs.get("name") for item in message_adapter.AnswerMessageAdapter.inputs}
    input_types = {item.kwargs.get("name"): item.__class__.__name__ for item in message_adapter.AnswerMessageAdapter.inputs}
    input_display_names = {item.kwargs.get("name"): item.kwargs.get("display_name") for item in message_adapter.AnswerMessageAdapter.inputs}

    assert "### 데이터 다운로드" in message
    assert "분석 결과 데이터 CSV 다운로드" in message
    assert "사용 원본 데이터: production_data CSV 다운로드" in message
    assert "http://localhost:8501/?download_ref=" in message
    assert "download_base_url" in input_names
    assert "show_download_links" in input_names
    assert "show_pandas_code" in input_names
    assert input_display_names["show_analysis_evidence"] == "중간 산출물/helper 결과 표시"
    for name in (
        "include_diagnostics",
        "show_result_table",
        "show_analysis_evidence",
        "show_download_links",
        "show_notices",
        "show_applied_criteria",
        "show_next_questions",
        "show_intent_analysis",
        "show_data_retrieval",
        "show_pandas_code",
    ):
        assert input_types[name] == "BoolInput"


def test_answer_message_adapter_default_download_link_uses_standalone_server():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "완료했습니다.",
        "data_refs": [
            {
                "store": "mongodb",
                "ref_id": "result:s1:abc",
                "collection_name": "agent_v4_result_store",
                "path": "payload.result_rows",
                "role": "analysis_result",
            }
        ],
    }

    message = message_adapter.build_message(payload)

    assert "http://localhost:8765/?download_ref=" in message


def test_answer_message_adapter_section_toggles_control_verbose_blocks():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "분석 완료입니다.",
        "intent_plan": {
            "analysis_kind": "pandas_analysis",
            "retrieval_jobs": [{"dataset_key": "production_today", "source_alias": "production_data"}],
            "pandas_execution_plan": [{"step": "집계"}],
        },
        "source_results": [{"dataset_key": "production_today", "source_alias": "production_data", "status": "ok", "row_count": 1}],
        "analysis": {
            "status": "ok",
            "row_count": 1,
            "columns": ["DEVICE", "QTY"],
            "analysis_code": "result = sources['production_data']",
            "step_outputs": [{"key": "basis", "row_count": 1, "columns": ["DEVICE"], "preview_rows": [{"DEVICE": "A"}]}],
            "function_case_results": [{"function_name": "sample_helper", "matched_count": 1, "preview_rows": [{"DEVICE": "A"}]}],
        },
        "data": {
            "columns": ["DEVICE", "QTY"],
            "rows": [{"DEVICE": "A", "QTY": 1}],
            "row_count": 1,
        },
        "data_refs": [{"ref_id": "result:s1:abc", "role": "analysis_result", "path": "payload.result_rows"}],
        "answer_sections": {
            "applied_criteria": {
                "datasets": [{"dataset_key": "production_today", "source_alias": "production_data"}],
                "required_params": {"production_data": {"DATE": "20260707"}},
            },
            "next_questions": ["제품별로 더 나눠볼까요?"],
        },
        "trace": {"warnings": [{"type": "demo", "message": "주의"}], "errors": [], "inspection": {"pandas_execution": {"status": "ok", "generated_code": "result = sources['production_data']"}}},
    }

    message = message_adapter.build_message(
        payload,
        "",
        "false",
        show_result_table="false",
        show_analysis_evidence="false",
        show_download_links="false",
        show_notices="false",
        show_applied_criteria="false",
        show_next_questions="false",
        show_intent_analysis="false",
        show_data_retrieval="false",
        show_pandas_code="false",
    )

    assert "### 답변" in message
    assert "### 결과 테이블" not in message
    assert "### 중간 분석 산출물" not in message
    assert "### helper 실행 결과" not in message
    assert "### 분석 과정 요약" not in message
    assert "### 분석 근거" not in message
    assert "### 데이터 다운로드" not in message
    assert "### 경고/오류" not in message
    assert "### 적용 기준" not in message
    assert "### 다음에 볼 만한 질문" not in message
    assert "### 의도 분석" not in message
    assert "### 데이터 조회" not in message
    assert "### pandas 코드/실행" not in message


def test_answer_message_adapter_rewrites_english_intent_reasons_to_korean_summary():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": "조건을 추가해 다시 조회했습니다.",
        "intent_plan": {
            "analysis_kind": "pandas_analysis",
            "request_scope": "followup_requery",
            "reuse_strategy": "previous_intent_with_new_retrieval",
            "condition_resolution": {
                "inherited": {"required_params": {"DATE": "20260707"}, "filters": {"OPER_NAME": "W/B"}},
                "new": {"filters": {"MCP_NO": {"operator": "starts_with", "value": "L-267"}}},
            },
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260707"},
                    "filters": {
                        "OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]},
                        "MCP_NO": {"operator": "starts_with", "value": "L-267"},
                    },
                }
            ],
            "pandas_execution_plan": [{"groupby_columns": ["DEVICE"], "aggregate_column": "PRODUCTION"}],
        },
        "data": {"columns": ["DEVICE", "PRODUCTION"], "rows": [], "row_count": 0},
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "intent": {
                    "analysis_kind": "pandas_analysis",
                    "retrieval_job_count": 1,
                    "pandas_step_count": 1,
                    "decision_reason": [
                        "The user is asking to filter the previous result based on 'MCP NO' starting with 'L-267'.",
                        "This is a follow-up query that modifies the filter conditions of the previous intent.",
                    ],
                }
            },
        },
    }

    message = message_adapter.build_message(payload, "", show_intent_analysis=True)

    assert "The user is asking" not in message
    assert "follow-up query" not in message
    assert "현재 질문은 이전 대화의 조건을 참고해야 하는 후속 질문으로 판단했습니다." in message
    assert "이전 의도 계획을 바탕으로 조건을 반영한 새 데이터 조회를 수행하도록 설정했습니다." in message
    assert "MCP_NO" in message
    assert "L-267" in message


def test_answer_message_adapter_toggles_strip_sections_embedded_in_answer_text():
    message_adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "21_answer_message_adapter.py")
    payload = {
        "answer_message": (
            "직접 답변입니다.\n\n"
            "### 결과 테이블\n"
            "| DEVICE | QTY |\n| --- | ---: |\n| A | 1 |\n\n"
            "### 분석 근거\n"
            "- 중간 집계 1건\n\n"
            "### 데이터 다운로드\n"
            "- CSV 다운로드\n\n"
            "### 적용 기준\n"
            "- production_today\n\n"
            "### 다음에 볼 만한 질문\n"
            "- 더 나눠볼까요?\n\n"
            "### 의도 분석\n"
            "- pandas_analysis\n\n"
            "### 데이터 조회\n"
            "- production_today 1건\n\n"
            "### pandas 코드/실행\n"
            "```python\nresult = df\n```"
        ),
        "data": {"columns": ["DEVICE", "QTY"], "rows": [{"DEVICE": "A", "QTY": 1}], "row_count": 1},
        "analysis": {"step_outputs": [{"key": "basis", "row_count": 1}]},
        "data_refs": [{"ref_id": "result:s1:abc", "role": "analysis_result"}],
        "intent_plan": {"analysis_kind": "pandas_analysis"},
        "source_results": [{"dataset_key": "production_today", "status": "ok", "row_count": 1}],
        "trace": {"inspection": {"pandas_execution": {"generated_code": "result = df"}}},
    }

    component = message_adapter.AnswerMessageAdapter()
    component.payload = payload
    component.show_result_table = False
    component.show_analysis_evidence = False
    component.show_download_links = False
    component.show_notices = False
    component.show_applied_criteria = False
    component.show_next_questions = False
    component.show_intent_analysis = False
    component.show_data_retrieval = False
    component.show_pandas_code = False

    message = component.build_output_message().text

    assert "직접 답변입니다." in message
    assert "### 결과 테이블" not in message
    assert "| DEVICE | QTY |" not in message
    assert "### 중간 분석 산출물" not in message
    assert "### helper 실행 결과" not in message
    assert "### 분석 근거" not in message
    assert "### 데이터 다운로드" not in message
    assert "### 적용 기준" not in message
    assert "### 다음에 볼 만한 질문" not in message
    assert "### 의도 분석" not in message
    assert "### 데이터 조회" not in message
    assert "### pandas 코드/실행" not in message
    assert "result = df" not in message


def test_api_response_builder_uses_chat_display_message_when_connected():
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "answer_message": "단순 답변입니다.",
        "analysis": {"status": "ok"},
        "data": {"columns": ["지표", "값"], "rows": [{"지표": "생산 실적", "값": 650}], "row_count": 1},
    }

    response = api_builder.build_api_response(payload, "### 답변\n상세 답변입니다.\n\n### 결과 테이블\n| 지표 | 값 |\n| --- | ---: |\n| 생산 실적 | 650 |")

    assert response["status"] == "ok"
    assert response["message"].startswith("### 답변")
    assert "answer_message" not in response
    assert "display_message" not in response
    assert response["data_mode"] == "live"


def test_api_response_builder_marks_error_when_one_required_source_fails():
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_data"},
                {"dataset_key": "wip", "source_alias": "wip_data"},
            ]
        },
        "source_results": [
            {"source_alias": "production_data", "status": "ok", "success": True, "errors": []},
            {"source_alias": "wip_data", "status": "error", "success": False, "errors": [{"type": "timeout"}]},
        ],
        "analysis": {"status": "ok"},
        "data": {"rows": [{"PRODUCTION": 10}], "columns": ["PRODUCTION"], "row_count": 1},
    }

    response = api_builder.build_api_response(payload)

    assert response["status"] == "error"
    assert response["stage_status"] == {"overall": "error", "retrieval": "error", "analysis": "ok"}


def test_api_response_builder_marks_partial_when_only_optional_source_fails():
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_data"},
                {"dataset_key": "uph", "source_alias": "uph_data", "required": False},
            ]
        },
        "source_results": [
            {"source_alias": "production_data", "status": "ok", "success": True, "errors": []},
            {"source_alias": "uph_data", "status": "error", "success": False, "errors": [{"type": "timeout"}]},
        ],
        "analysis": {"status": "ok"},
        "data": {"rows": [{"PRODUCTION": 10}], "columns": ["PRODUCTION"], "row_count": 1},
    }

    response = api_builder.build_api_response(payload)

    assert response["status"] == "partial"
    assert response["stage_status"] == {"overall": "partial", "retrieval": "partial", "analysis": "ok"}


def test_api_response_builder_marks_error_when_required_source_is_missing():
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "intent_plan": {"retrieval_jobs": [{"dataset_key": "wip", "source_alias": "wip_data"}]},
        "source_results": [],
        "analysis": {"status": "ok"},
        "data": {"rows": [], "columns": [], "row_count": 0},
    }

    response = api_builder.build_api_response(payload)

    assert response["status"] == "error"
    assert response["stage_status"]["retrieval"] == "error"


def test_pandas_executor_outputs_json_ready_numeric_rows():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {"runtime_sources": {}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    result = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                "result = pd.DataFrame({"
                "'DEVICE': ['DEV-A'], "
                "'QTY': pd.Series([7], dtype='int64'), "
                "'RATIO': pd.Series([1.5], dtype='float64'), "
                "'EMPTY': [float('nan')]"
                "})"
            )
        },
    )

    json.dumps(result["data"], ensure_ascii=False)
    assert result["data"]["rows"] == [{"DEVICE": "DEV-A", "EMPTY": None, "QTY": 7, "RATIO": 1.5}]
    assert result["_full_result_rows"] == [{"DEVICE": "DEV-A", "EMPTY": None, "QTY": 7, "RATIO": 1.5}]
    assert "_runtime_result_rows" not in result


def test_pandas_executor_and_repair_are_not_invoked_after_required_retrieval_failure():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_calls = []
    payload = {
        "execution_gate": {"status": "blocked"},
        "analysis": {
            "status": "error",
            "error": {"type": "required_source_retrieval_failed", "message": "required source failed"},
        },
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": "raise RuntimeError('must not run')"},
        repair_invoker=lambda prompt: repair_calls.append(prompt),
    )

    assert repair_calls == []
    assert result["analysis"]["error"]["type"] == "required_source_retrieval_failed"
    assert result["trace"]["inspection"]["pandas_execution"]["status"] == "skipped"
    assert result["trace"]["inspection"]["pandas_repair"]["llm_called"] is False


def test_pandas_executor_wraps_scalar_result_with_meaningful_columns():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "request": {"question": "전일 L-218K8H 제품의 SBM공정에서 생산 실적 알려줘"},
        "runtime_sources": {},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = pandas_executor.execute_pandas_code(payload, {"code": "result = 650"})

    assert result["data"]["columns"] == ["지표", "값"]
    assert result["data"]["rows"] == [{"지표": "생산 실적", "값": 650}]
    assert result["analysis"]["columns"] == ["지표", "값"]


def test_pandas_executor_trace_preview_is_compact_but_full_rows_are_kept():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {"runtime_sources": {}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    result = pandas_executor.execute_pandas_code(
        payload,
        {"code": "result = pd.DataFrame({'idx': list(range(12))})"},
    )
    trace_rows = result["trace"]["inspection"]["pandas_execution"]["execution_result"]["preview_rows"]

    assert result["analysis"]["row_count"] == 12
    assert len(result["_full_result_rows"]) == 12
    assert len(result["data"]["rows"]) == 12
    assert len(trace_rows) == 5


def test_pandas_executor_records_step_and_function_case_outputs():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "production_data": [
                {"DEVICE": "DEV-A", "WIP": 12000, "PRODUCTION": 7},
                {"DEVICE": "DEV-B", "WIP": 3000, "PRODUCTION": 3},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    result = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                "df = sources['production_data'].copy()\n"
                "top = df.sort_values('WIP', ascending=False).head(1)\n"
                "record_step('top_wip_product', top, description='현재 재공이 가장 많은 제품', role='basis')\n"
                "record_function_case_result('sample_helper', 'DEV-A', top, description='helper 결과')\n"
                "result = top[['DEVICE', 'WIP']]"
            )
        },
    )

    step_outputs = result["analysis"]["step_outputs"]
    function_case_results = result["analysis"]["function_case_results"]

    assert result["analysis"]["status"] == "ok"
    assert step_outputs[0]["key"] == "top_wip_product"
    assert step_outputs[0]["preview_rows"][0]["DEVICE"] == "DEV-A"
    assert function_case_results[0]["function_name"] == "sample_helper"
    assert function_case_results[0]["matched_count"] == 1
    assert "step_outputs" not in result["trace"]["inspection"]["pandas_execution"]


def test_answer_variables_accept_numpy_scalars_after_result_store(monkeypatch):
    import numpy as np

    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    answer_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "18_answer_variables_builder.py")
    payload = {
        "request": {"session_id": "s1", "question": "수량 알려줘"},
        "runtime_sources": {"production": [{"DEVICE": "DEV-A", "QTY": np.int64(7)}]},
        "_full_result_rows": [{"DEVICE": "DEV-A", "QTY": np.int64(7), "RATIO": np.float64(1.5), "EMPTY": np.nan}],
        "source_results": [{"source_alias": "production", "row_count": np.int64(1), "preview_rows": [{"DEVICE": "DEV-A"}]}],
        "analysis": {"status": "ok", "row_count": np.int64(1), "rows": [{"SHOULD_NOT_STORE": True}]},
        "data": {
            "columns": ["DEVICE", "QTY", "RATIO", "EMPTY"],
            "rows": [{"DEVICE": "DEV-A", "QTY": np.int64(7), "RATIO": np.float64(1.5), "EMPTY": np.nan}],
            "row_count": np.int64(1),
        },
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "pandas_execution": {
                    "generated_code": "result = sources['production']",
                    "effective_code_with_helpers": "def helper(): pass\nresult = sources['production']",
                    "helper_sources": {"helper": "def helper(): pass"},
                    "used_helpers": ["match_product_tokens"],
                    "execution_result": {"row_count": np.int64(1), "columns": ["DEVICE"], "preview_rows": [{"DEVICE": "DEV-A"}]},
                }
            },
        },
    }

    stored = result_store.store_result(
        payload,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
    )
    variables = answer_variables.build_variables(stored)
    result_summary = json.loads(variables["result_summary_json"])
    applied_scope = json.loads(variables["applied_scope_json"])
    answer_context = json.loads(variables["answer_context_json"])
    ref_id = stored["data"]["data_ref"]["ref_id"]

    assert variables["question"] == "수량 알려줘"
    assert result_summary["rows"][0] == {"DEVICE": "DEV-A", "EMPTY": None, "QTY": 7, "RATIO": 1.5}
    assert applied_scope["pandas_execution"]["row_count"] == 1
    assert applied_scope["pandas_execution"]["used_helpers"] == ["match_product_tokens"]
    assert answer_context["number_display_policy"]["gte_10000"] == "k_unit"
    assert answer_context["result_shape"]["row_count"] == 1
    assert "generated_code" not in variables["applied_scope_json"]
    assert "effective_code_with_helpers" not in variables["applied_scope_json"]
    assert "helper_sources" not in variables["applied_scope_json"]
    assert "preview_rows" not in variables["applied_scope_json"]
    stored_payload = mongo_store["datagov"]["agent_v4_result_store"][ref_id]["payload"]
    assert stored_payload["result_rows"][0]["QTY"] == 7
    assert "rows" not in stored_payload["data"]
    assert "rows" not in stored_payload["analysis"]


def test_result_store_accepts_legacy_runtime_result_rows(monkeypatch):
    import numpy as np

    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    payload = {
        "request": {"session_id": "s1", "question": "수량 알려줘"},
        "_runtime_result_rows": [{"DEVICE": "LEGACY", "QTY": np.int64(3)}],
        "data": {"columns": ["DEVICE", "QTY"], "rows": [{"DEVICE": "PREVIEW", "QTY": 1}], "row_count": 1},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    stored = result_store.store_result(
        payload,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
    )
    ref_id = stored["data"]["data_ref"]["ref_id"]

    assert mongo_store["datagov"]["agent_v4_result_store"][ref_id]["payload"]["result_rows"] == [{"DEVICE": "LEGACY", "QTY": 3}]


def test_pandas_executor_uses_shared_namespace_for_comprehensions():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "runtime_sources": {
            "production_data": [
                {"OPER_NAME": "D/A1", "PRODUCTION": 10},
                {"OPER_NAME": "D/A2", "PRODUCTION": 20},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    llm_response = {
        "code": (
            "df_production_data = sources['production_data']\n"
            "group_by_cols = ['OPER_NAME']\n"
            "if all(col in df_production_data.columns for col in group_by_cols):\n"
            "    result = df_production_data.groupby(group_by_cols)['PRODUCTION'].sum().reset_index()\n"
            "else:\n"
            "    result = pd.DataFrame(columns=group_by_cols + ['PRODUCTION'])"
        )
    }

    result = pandas_executor.execute_pandas_code(payload, llm_response)

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["row_count"] == 2


def test_pandas_variables_use_source_result_columns_when_runtime_rows_are_empty():
    pandas_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py")
    payload = {
        "source_results": [
            {
                "source_alias": "production_data",
                "dataset_key": "production",
                "columns": ["WORK_DATE", "OPER_NAME", "TECH", "DENSITY", "MODE", "PRODUCTION"],
                "row_count": 0,
            }
        ],
        "runtime_sources": {"production_data": []},
    }

    variables = pandas_variables.build_variables(payload)
    schema = json.loads(variables["source_schema_json"])

    assert schema["production_data"] == ["WORK_DATE", "OPER_NAME", "TECH", "DENSITY", "MODE", "PRODUCTION"]


def test_pandas_executor_keeps_empty_source_columns_from_source_results():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "source_results": [
            {
                "source_alias": "production_data",
                "dataset_key": "production",
                "columns": ["TECH", "DENSITY", "MODE", "PRODUCTION"],
                "row_count": 0,
            }
        ],
        "runtime_sources": {"production_data": []},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    llm_response = {
        "code": (
            "df = sources['production_data']\n"
            "result = df.groupby(['TECH', 'DENSITY', 'MODE'])['PRODUCTION'].sum().reset_index(name='생산량')"
        )
    }

    result = pandas_executor.execute_pandas_code(payload, llm_response)

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["columns"] == ["TECH", "DENSITY", "MODE", "생산량"]
    assert result["data"]["row_count"] == 0


def test_intent_and_pandas_variables_expose_selected_function_case_context():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    pandas_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py")
    payload = {"request": {"question": "RG 32G DDR4 FBGA 96 DDP 제품 BG공정 생산량 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    normalized = intent_normalizer.normalize_intent_plan(
        payload,
        {
            "intent_plan": {
                "analysis_kind": "product_token_analysis",
                "pandas_function_case": {
                    "key": "product_token_match",
                    "function_name": "match_product_tokens",
                    "input_text": "RG 32G DDR4 FBGA 96 DDP",
                },
                "retrieval_jobs": [{"dataset_key": "production_today", "source_alias": "production_data"}],
                "pandas_execution_plan": [{"step": "sum_production", "source_alias": "production_data"}],
            }
        },
    )

    assert normalized["intent_plan"]["pandas_execution_plan"][0]["operation"] == "apply_pandas_function_case"
    assert "pandas_function_case" not in normalized["intent_plan"]
    assert "selected_function_cases" not in normalized["intent_plan"]
    assert normalized["intent_plan"]["pandas_function_cases"] == [
        {
            "key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "RG 32G DDR4 FBGA 96 DDP",
            "source_alias": "production_data",
        }
    ]
    variables = pandas_variables.build_variables(normalized)
    context = json.loads(variables["function_case_selection_json"])

    assert context["available_helpers"][0]["function_name"] == "match_product_tokens"
    assert "selected_case" not in context
    assert context["selected_steps"][0]["input_text"] == "RG 32G DDR4 FBGA 96 DDP"


def test_multiple_function_cases_expose_multiple_helpers_and_dummy_runtime():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    pandas_variables = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py")
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "request": {"question": "RG 32G DDR4 FBGA 96 DDP 제품 BG공정 생산량 알려줘"},
        "runtime_sources": {
            "production_data": [
                {"TECH": "RG", "DEN": "32G", "MODE": "DDR4", "PKG_TYPE1": "FBGA", "PKG_TYPE2": "DDP", "LEAD": "96", "DEVICE": "DEV-RG", "PRODUCTION": 10},
                {"TECH": "XX", "DEN": "16G", "MODE": "DDR5", "PKG_TYPE1": "BGA", "PKG_TYPE2": "SDP", "LEAD": "78", "DEVICE": "DEV-XX", "PRODUCTION": 99},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    normalized = intent_normalizer.normalize_intent_plan(
        payload,
        {
            "intent_plan": {
                "analysis_kind": "multi_function_case_demo",
                "pandas_function_cases": [
                    {
                        "key": "product_token_match",
                        "function_name": "match_product_tokens",
                        "input_text": "RG 32G DDR4 FBGA 96 DDP",
                        "source_alias": "production_data",
                    },
                    {
                        "key": "sample_passthrough_demo",
                        "function_name": "sample_passthrough_helper",
                        "input_text": "format demo",
                        "source_alias": "production_data",
                    },
                ],
                "retrieval_jobs": [{"dataset_key": "production_today", "source_alias": "production_data"}],
                "pandas_execution_plan": [{"step": "sum_production", "source_alias": "production_data"}],
            }
        },
    )
    variables = pandas_variables.build_variables(normalized)
    context = json.loads(variables["function_case_selection_json"])
    helper_names = [item["function_name"] for item in context["available_helpers"]]

    assert [step["function_name"] for step in normalized["intent_plan"]["pandas_execution_plan"][:2]] == ["match_product_tokens", "sample_passthrough_helper"]
    assert helper_names == ["match_product_tokens", "sample_passthrough_helper"]

    result = pandas_executor.execute_pandas_code(
        normalized,
        {
            "code": (
                function_case_source("match_product_tokens", "sample_passthrough_helper")
                + "\n\n"
                "df = match_product_tokens('RG 32G DDR4 FBGA 96 DDP', sources['production_data'])\n"
                "df = sample_passthrough_helper('format demo', df)\n"
                "result = df[['DEVICE', 'PRODUCTION']]"
            )
        },
    )

    trace = result["trace"]["inspection"]["pandas_execution"]
    assert result["data"]["rows"] == [{"DEVICE": "DEV-RG", "PRODUCTION": 10}]
    assert trace["used_helpers"] == ["match_product_tokens", "sample_passthrough_helper"]
    assert "def sample_passthrough_helper" in trace["generated_code"]


def test_intent_normalizer_dedupes_single_and_multiple_function_cases():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "제품 token 분석"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    normalized = intent_normalizer.normalize_intent_plan(
        payload,
        {
            "intent_plan": {
                "analysis_kind": "product_token_analysis",
                "pandas_function_case": {
                    "key": "product_token_match",
                    "function_name": "match_product_tokens",
                    "input_text": "RG 32G DDR4 FBGA 96 DDP",
                    "source_alias": "production_data",
                },
                "pandas_function_cases": [
                    {
                        "key": "product_token_match",
                        "function_name": "match_product_tokens",
                        "input_text": "RG 32G DDR4 FBGA 96 DDP",
                        "source_alias": "production_data",
                    }
                ],
                "selected_function_cases": [{"key": "legacy"}],
                "retrieval_jobs": [{"dataset_key": "production_today", "source_alias": "production_data"}],
                "pandas_execution_plan": [{"step": "sum_production", "source_alias": "production_data"}],
            }
        },
    )

    assert "pandas_function_case" not in normalized["intent_plan"]
    assert "selected_function_cases" not in normalized["intent_plan"]
    assert normalized["intent_plan"]["pandas_function_cases"] == [
        {
            "key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "RG 32G DDR4 FBGA 96 DDP",
            "source_alias": "production_data",
        }
    ]
    assert [step["operation"] for step in normalized["intent_plan"]["pandas_execution_plan"][:1]] == ["apply_pandas_function_case"]


def test_intent_normalizer_preserves_followup_scope_and_allows_previous_result_reuse():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "상위 3개만 보여줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    normalized = intent_normalizer.normalize_intent_plan(
        payload,
        {
            "intent_plan": {
                "analysis_kind": "result_top_n",
                "request_scope": "followup_transform",
                "reuse_strategy": "previous_result",
                "condition_resolution": {
                    "inherited": {"metric": "생산량"},
                    "changed": {"limit": 3},
                },
                "retrieval_jobs": [],
                "pandas_execution_plan": [{"step": "이전 결과에서 상위 3개 선택"}],
            }
        },
    )

    assert normalized["intent_plan"]["request_scope"] == "followup_transform"
    assert normalized["intent_plan"]["reuse_strategy"] == "previous_result"
    assert normalized["intent_plan"]["condition_resolution"]["changed"] == {"limit": 3}
    assert normalized["trace"]["inspection"]["intent"]["status"] == "ok"
    assert normalized["trace"]["inspection"]["intent"]["previous_data_reuse"] is True
    assert not [item for item in normalized["trace"]["warnings"] if item.get("type") == "missing_retrieval_jobs"]


def test_intent_normalizer_warns_when_followup_requery_has_no_retrieval_jobs():
    intent_normalizer = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04_intent_plan_normalizer.py")
    payload = {"request": {"question": "어제 생산량은?"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    normalized = intent_normalizer.normalize_intent_plan(
        payload,
        {
            "intent_plan": {
                "analysis_kind": "production_sum",
                "request_scope": "followup_requery",
                "reuse_strategy": "previous_intent_with_new_retrieval",
                "retrieval_jobs": [],
                "pandas_execution_plan": [],
            }
        },
    )

    assert normalized["trace"]["inspection"]["intent"]["status"] == "warning"
    assert [item for item in normalized["trace"]["warnings"] if item.get("type") == "missing_retrieval_jobs"]


def test_specialized_function_examples_match_runtime_and_domain_saving_contracts():
    removed_domain_md = (
        ROOT
        / "langflow_components"
        / "domain_saving_flow"
        / "pandas_function_cases_raw_text_input_example.md"
    )
    removed_context_json = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "function_case_context_json_input_example.json"
    )
    domain_text = (ROOT / "domain_knowledge.txt").read_text(encoding="utf-8")
    helper_code = function_case_source()

    assert not removed_domain_md.exists()
    assert not removed_context_json.exists()
    assert "pandas function case 등록 규칙" in domain_text
    assert "section은 pandas_function_cases이고 key는 product_token_match" in domain_text
    assert "function_name은 match_product_tokens" in domain_text
    assert "section은 pandas_function_cases이고 key는 sample_passthrough_demo" in domain_text

    assert "def match_product_tokens" in helper_code
    assert "def sample_passthrough_helper" in helper_code
    assert "def record_function_case_result" in helper_code
    assert "source_code_lines" not in helper_code


def test_integrated_pandas_repair_skips_llm_after_initial_success():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    calls: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"production_data": [{"DEVICE": "DEV001", "PRODUCTION": 10}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    def unexpected_repair(prompt: str):
        calls.append(prompt)
        raise AssertionError("repair LLM must not run after initial success")

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": "result = sources['production_data']"},
        repair_invoker=unexpected_repair,
        repair_prompt_template="unused {failed_code}",
    )

    repair_trace = result["trace"]["inspection"]["pandas_repair"]
    assert result["analysis"]["status"] == "ok"
    assert calls == []
    assert repair_trace["attempted"] is False
    assert repair_trace["llm_called"] is False
    assert repair_trace["selected"] == "initial"


def test_pandas_executor_supports_zip_builtin_without_repair():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    calls: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"plan_actual": [{"PLAN": 100, "ACTUAL": 50}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    code = (
        "df = sources['plan_actual'].copy()\n"
        "rename_map = dict(zip(['PLAN', 'ACTUAL'], ['계획', '실적']))\n"
        "result = df.rename(columns=rename_map)"
    )

    def unexpected_repair(prompt: str):
        calls.append(prompt)
        raise AssertionError("safe zip builtin must avoid repair")

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": code},
        repair_invoker=unexpected_repair,
        repair_prompt_template="unused {failed_code}",
    )

    repair = result["trace"]["inspection"]["pandas_repair"]
    assert result["analysis"]["status"] == "ok"
    assert result["data"]["rows"] == [{"계획": 100, "실적": 50}]
    assert calls == []
    assert repair["attempted"] is False
    assert repair["llm_called"] is False


def test_pandas_executor_normalizes_exact_pandas_import_for_hold_history_without_repair():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    calls: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {
            "hold_history_data": [
                {"LOT_ID": "L1", "HOLD_TM": "2026-07-01 08:00", "HOLD_CD": "H001", "HOLD_DESC": "first"},
                {"LOT_ID": "L2", "HOLD_TM": "2026-07-02 08:00", "HOLD_CD": "H002", "HOLD_DESC": "latest"},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    code = (
        "import pandas as pd\n\n"
        "df_hold_history = sources['hold_history_data']\n"
        "df_sorted_history = df_hold_history.sort_values(by='HOLD_TM', ascending=False)\n"
        "columns_map = {'LOT_ID': 'LOT ID', 'HOLD_TM': 'HOLD 발생 시간', 'HOLD_CD': 'HOLD 코드', 'HOLD_DESC': 'HOLD 상세 사유'}\n"
        "existing_columns = [col for col in columns_map if col in df_sorted_history.columns]\n"
        "result_df = df_sorted_history[existing_columns].rename(columns=columns_map)\n"
        "final_columns_order = ['LOT ID', 'HOLD 발생 시간', 'HOLD 코드', 'HOLD 상세 사유']\n"
        "result = result_df[[col for col in final_columns_order if col in result_df.columns]]"
    )

    def unexpected_repair(prompt: str):
        calls.append(prompt)
        raise AssertionError("safe pandas import normalization must avoid repair")

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": code},
        repair_invoker=unexpected_repair,
        repair_prompt_template="unused {failed_code}",
    )

    execution = result["trace"]["inspection"]["pandas_execution"]
    repair = result["trace"]["inspection"]["pandas_repair"]
    assert result["analysis"]["status"] == "ok"
    assert calls == []
    assert [row["LOT ID"] for row in result["data"]["rows"]] == ["L2", "L1"]
    assert "import pandas as pd" not in execution["generated_code"]
    assert execution["safe_import_normalization"]["removed_imports"] == ["import pandas as pd"]
    assert execution["safe_import_normalization"]["provided_namespaces"] == ["pd"]
    assert repair["attempted"] is False
    assert repair["llm_called"] is False

    import_only = pandas_executor.execute_pandas_code(payload, {"code": "import pandas as pd\n"})
    assert import_only["analysis"]["status"] == "error"
    assert import_only["analysis"]["error"]["type"] == "missing_code"
    assert import_only["trace"]["inspection"]["pandas_execution"]["safe_import_normalization"]["removed_imports"] == [
        "import pandas as pd"
    ]


def test_pandas_executor_supports_exact_numpy_alias_with_restricted_namespace():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"plan_actual": [{"PLAN": 100, "ACTUAL": 50}, {"PLAN": 0, "ACTUAL": 10}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    result = pandas_executor.execute_pandas_code(
        payload,
        {
            "code": (
                "import numpy as np\n"
                "df = sources['plan_actual'].copy()\n"
                "df['RATE'] = np.where(df['PLAN'].ne(0), df['ACTUAL'].div(df['PLAN']).mul(100), 0)\n"
                "result = df"
            )
        },
    )

    execution = result["trace"]["inspection"]["pandas_execution"]
    assert result["analysis"]["status"] == "ok"
    assert [row["RATE"] for row in result["data"]["rows"]] == [50.0, 0.0]
    assert execution["safe_import_normalization"]["removed_imports"] == ["import numpy as np"]
    assert execution["safe_import_normalization"]["provided_namespaces"] == ["pd", "np_safe"]


def test_pandas_executor_keeps_other_imports_and_io_apis_blocked():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"data": [{"A": 1}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    unsafe_codes = (
        "import os\nresult = sources['data']",
        "from pandas import DataFrame\nresult = sources['data']",
        "import pandas as pd, os\nresult = sources['data']",
        "import numpy.random as rnd\nresult = sources['data']",
        "import pandas as pd; import os\nresult = sources['data']",
        "result = pd.read_csv('https://example.invalid/data.csv')",
        "import numpy as np\nresult = np.load('data.npy', allow_pickle=True)",
        "import numpy as np\nnp.where([True], [1], [0]).tofile('data.bin')\nresult = sources['data']",
    )
    for code in unsafe_codes:
        result = pandas_executor.execute_pandas_code(payload, {"code": code})
        assert result["analysis"]["status"] == "error", code
        assert result["analysis"]["error"]["type"] == "unsafe_code", code

    string_literal = pandas_executor.execute_pandas_code(
        payload,
        {"code": "note = '''\nimport pandas as pd\n'''\nresult = sources['data']"},
    )
    assert string_literal["analysis"]["status"] == "ok"
    assert string_literal["trace"]["inspection"]["pandas_execution"]["safe_import_normalization"] == {}


def test_integrated_pandas_repair_receives_non_whitelisted_import_error():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_template = (ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    prompts: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"data": [{"A": 1}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    def repair_once(prompt: str):
        prompts.append(prompt)
        return {"code": "result = sources['data']"}

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": "import os\nresult = sources['data']"},
        repair_invoker=repair_once,
        repair_prompt_template=repair_template,
    )

    repair = result["trace"]["inspection"]["pandas_repair"]
    assert result["analysis"]["status"] == "ok"
    assert len(prompts) == 1
    assert "import os" in prompts[0]
    assert "import 문은 허용하지 않습니다" in prompts[0]
    assert repair["attempted"] is True
    assert repair["llm_called"] is True
    assert repair["selected"] == "retry"


def test_integrated_pandas_repair_passes_failed_code_and_error_then_selects_clean_retry():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_template = (ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    helper_code = "def selected_helper(df):\n    return df"
    prompts: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"production_data": [{"DEVICE": "DEV001", "PRODUCTION": 10}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    def repair_once(prompt: str):
        prompts.append(prompt)
        return {"code": "result = sources['production_data']"}

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": "result = sources['missing_data']"},
        repair_invoker=repair_once,
        repair_prompt_template=types.SimpleNamespace(text=repair_template),
        function_case_helper_code=types.SimpleNamespace(text=helper_code),
        max_repair_attempts=1,
    )

    repair_trace = result["trace"]["inspection"]["pandas_repair"]
    assert len(prompts) == 1
    assert "result = sources['missing_data']" in prompts[0]
    assert "KeyError" in prompts[0]
    assert '"executed_code_with_preamble"' in prompts[0]
    assert '"traceback_summary"' in prompts[0]
    assert helper_code in prompts[0]
    assert result["analysis"]["status"] == "ok"
    assert result["analysis"]["repair_applied"] is True
    assert result["pandas_retry_attempt"] == 1
    assert result["data"]["rows"] == [{"DEVICE": "DEV001", "PRODUCTION": 10}]
    assert result["trace"].get("errors") == []
    assert repair_trace["attempted"] is True
    assert repair_trace["llm_called"] is True
    assert repair_trace["selected"] == "retry"
    assert repair_trace["initial_error"]["type"] == "pandas_execution_error"
    assert repair_trace["initial_code_sha256"]
    assert repair_trace["initial_code_preview"] == "result = sources['missing_data']"


def test_integrated_pandas_repair_attempts_only_once_and_keeps_both_failures():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_template = (ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    calls = 0
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {"production_data": [{"DEVICE": "DEV001", "PRODUCTION": 10}]},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    def failed_repair(_prompt: str):
        nonlocal calls
        calls += 1
        return {"code": "result = sources['missing_again']"}

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": "result = sources['missing_initial']"},
        repair_invoker=failed_repair,
        repair_prompt_template=repair_template,
        max_repair_attempts=2,
    )

    repair_trace = result["trace"]["inspection"]["pandas_repair"]
    assert calls == 1
    assert result["analysis"]["status"] == "error"
    assert "missing_again" in result["analysis"]["error"]["message"]
    assert "missing_initial" in repair_trace["initial_error"]["message"]
    assert "missing_again" in repair_trace["retry_error"]["message"]
    assert repair_trace["max_attempts"] == 1
    assert repair_trace["attempt"] == 1
    assert repair_trace["selected"] == "retry_error"


def test_pandas_executor_np_name_error_uses_integrated_single_ratio_repair():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_template = (ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    prompts: list[str] = []
    payload = {
        "intent_plan": {"retrieval_jobs": [], "pandas_execution_plan": []},
        "runtime_sources": {
            "plan_actual": [
                {"PLAN": 100, "ACTUAL": 50},
                {"PLAN": 0, "ACTUAL": 10},
                {"PLAN": None, "ACTUAL": 5},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    initial_code = (
        "df = sources['plan_actual'].copy()\n"
        "denominator = df['PLAN']\n"
        "df['RATE'] = np.where(denominator.ne(0), df['ACTUAL'].div(denominator).mul(100), 0)\n"
        "result = df"
    )

    def repair_once(prompt: str):
        prompts.append(prompt)
        return {
            "code": (
                "df = sources['plan_actual'].copy()\n"
                "denominator = pd.to_numeric(df['PLAN'], errors='coerce')\n"
                "numerator = pd.to_numeric(df['ACTUAL'], errors='coerce')\n"
                "df['RATE'] = numerator.div(denominator).mul(100).where(denominator.ne(0), 0).fillna(0)\n"
                "result = df[['PLAN', 'ACTUAL', 'RATE']]"
            )
        }

    result = pandas_executor.execute_pandas_with_repair(
        payload,
        {"code": initial_code},
        repair_invoker=repair_once,
        repair_prompt_template=repair_template,
    )

    repair_trace = result["trace"]["inspection"]["pandas_repair"]
    assert len(prompts) == 1
    assert "NameError" in prompts[0] and "np" in prompts[0]
    assert initial_code in prompts[0]
    assert result["analysis"]["status"] == "ok"
    assert [row["RATE"] for row in result["data"]["rows"]] == [50.0, 0.0, 0.0]
    assert repair_trace["attempted"] is True
    assert repair_trace["selected"] == "retry"


def test_pandas_filter_preamble_handles_compound_null_empty_filters_and_repair_scope():
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    repair_template = (ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "production",
                    "filters": {
                        "MODE": {"operator": "starts_with_any", "value": ["LP"]},
                        "PKG_TYPE1": {"operator": "in", "value": ["LFBGA", "TFBGA", "UFBGA", "VFBGA", "WFBGA"]},
                        "MCP_NO": {"operator": "or", "value": [{"operator": "isNull"}, {"operator": "isEmpty"}]},
                    },
                }
            ],
            "pandas_execution_plan": [],
        },
        "runtime_sources": {
            "production": [
                {"MODE": "LPDDR5", "PKG1": "LFBGA", "MCP_NO": "", "DEVICE": "MOBILE-1", "PRODUCTION": 10},
                {"MODE": "LPDDR5", "PKG1": "LFBGA", "MCP_NO": "P-001", "DEVICE": "POP-1", "PRODUCTION": 99},
                {"MODE": "DDR4", "PKG1": "FBGA", "MCP_NO": "", "DEVICE": "OTHER-1", "PRODUCTION": 88},
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    bad_llm_code = "if True:\nresult = sources['production']"

    failed = pandas_executor.execute_pandas_code(payload, {"code": bad_llm_code})
    repair_prompt = pandas_executor.build_pandas_repair_prompt(failed, repair_template)
    generated_code = failed["trace"]["inspection"]["pandas_execution"]["generated_code"]

    assert failed["analysis"]["status"] == "error"
    assert "expected an indented block" in failed["analysis"]["error"]["message"]
    assert bad_llm_code in repair_prompt
    assert "_filtered_source_1_production" in repair_prompt
    assert "동일한 pandas filter preamble을 retry 코드에 다시 자동 적용" in repair_prompt
    assert "if _filter_col_1_1:\n    _filter_col_1_2" not in generated_code
    assert ".str.startswith" in generated_code
    assert ".isna()" in generated_code
    assert ".str.strip().eq('')" in generated_code

    retry = pandas_executor.execute_pandas_code(failed, {"code": "result = sources['production'][['DEVICE', 'PRODUCTION']]"})

    assert retry["analysis"]["status"] == "ok"
    assert retry["data"]["rows"] == [{"DEVICE": "MOBILE-1", "PRODUCTION": 10}]


def test_langflow_dummy_data_covers_representative_manufacturing_cases():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {"dataset_key": "production_today", "source_alias": "production_today", "required_params": {"DATE": "20260701"}},
                    {"dataset_key": "production", "source_alias": "production", "required_params": {"DATE": "20260630"}},
                    {"dataset_key": "wip", "source_alias": "wip", "required_params": {"DATE": "20260630"}},
                    {"dataset_key": "wip", "source_alias": "wip_boh_0627", "required_params": {"DATE": "20260626"}},
                ],
            }
        }
    )
    results = {item["source_alias"]: item["rows"] for item in payload["source_results"]}

    assert any(row["OPER_NAME"] == "INPUT" and str(row["MCP_NO"]).startswith("L-267") for row in results["production_today"])
    assert any(row["OPER_NAME"] == "FCB/H" and row["DEVICE"] == "DEV-SP-DDR5" for row in results["production"])
    assert any(row["OPER_NAME"] == "SBM" and row["MCP_NO"] == "L-218K8H" for row in results["production"])
    assert any(row["OPER_NAME"].startswith("W/B") and row["FAMILY"] == "HBM" for row in results["wip"])
    assert any(row["OPER_NAME"] == "D/A1" and row["DEVICE"] == "DEV-DA-GDDR6" for row in results["wip"])
    assert any(row["OPER_NAME"].startswith("W/B") for row in results["wip_boh_0627"])


def test_langflow_dummy_production_fixture_has_discriminating_pkg_out_and_fcbh_rows():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {
                        "dataset_key": "production",
                        "source_alias": "production",
                        "required_params": {"DATE": "20260630"},
                    }
                ],
            }
        }
    )
    rows = payload["source_results"][0]["rows"]

    mobile_pkg_out = {
        row["DEVICE"]: row["PRODUCTION"]
        for row in rows
        if row["OPER_NAME"] == "PKG OUT"
        and str(row["MODE"]).startswith("LP")
        and row["PKG1"] in {"LFBGA", "TFBGA", "UFBGA", "VFBGA", "WFBGA"}
        and not str(row["MCP_NO"] or "").strip()
    }
    hbm_fcbh = [
        row["PRODUCTION"]
        for row in rows
        if row["OPER_NAME"] == "FCB/H" and row["DEVICE"] == "DEV-HBM"
    ]
    zero_fcbh = [
        row
        for row in rows
        if row["OPER_NAME"] == "FCB/H" and row["DEVICE"] == "DEV-FCBH-ZERO"
    ]

    assert mobile_pkg_out == {"DEV002": 504, "DEV-MOBILE-PKGOUT-B": 420}
    assert sorted(hbm_fcbh) == [17, 423]
    assert sum(hbm_fcbh) == 440
    assert len(zero_fcbh) == 1
    assert zero_fcbh[0]["PRODUCTION"] == 0


def test_langflow_dummy_fixture_keeps_outer_join_and_product_match_negative_controls():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {
                        "dataset_key": "production",
                        "source_alias": "production_0627",
                        "required_params": {"DATE": "20260627"},
                    },
                    {
                        "dataset_key": "wip",
                        "source_alias": "wip_0626",
                        "required_params": {"DATE": "20260626"},
                    },
                    {
                        "dataset_key": "production",
                        "source_alias": "production_0630",
                        "required_params": {"DATE": "20260630"},
                    },
                    {
                        "dataset_key": "production_today",
                        "source_alias": "production_0701",
                        "required_params": {"DATE": "20260701"},
                    },
                    {
                        "dataset_key": "wip_today",
                        "source_alias": "wip_0701",
                        "required_params": {"DATE": "20260701"},
                    },
                ],
            }
        }
    )
    results = {item["source_alias"]: item["rows"] for item in payload["source_results"]}

    production_wb = {
        row["OPER_NAME"]
        for row in results["production_0627"]
        if row["OPER_NAME"].startswith("W/B")
    }
    wip_wb = {
        row["OPER_NAME"]
        for row in results["wip_0626"]
        if row["OPER_NAME"].startswith("W/B")
    }
    sp_decoys = {
        row["DEVICE"]: (row["ORG"], row["LEAD"], row["PKG1"], row["PRODUCTION"])
        for row in results["production_0630"]
        if row["DEVICE"].startswith("DEV-SP-DECOY-")
    }
    mcp_decoys = {
        row["DEVICE"]: (row["MCP_NO"], row["PRODUCTION"])
        for row in results["production_0630"]
        if row["DEVICE"] == "DEV-L218-PREFIX-DECOY"
    }
    expected_rg_decoys = {
        "DEV-RG-DECOY-LEAD78",
        "DEV-RG-DECOY-DEN16",
        "DEV-RG-DECOY-FCBGA",
    }

    assert production_wb == {"W/B2", "W/B3", "W/B4", "W/B5", "W/B6"}
    assert wip_wb == {"W/B1", "W/B2", "W/B3", "W/B4", "W/B5"}
    assert sp_decoys == {
        "DEV-SP-DECOY-X8": ("8", "78", "FCBGA", 2000),
        "DEV-SP-DECOY-LEAD96": ("4", "96", "FCBGA", 2100),
        "DEV-SP-DECOY-VFBGA": ("4", "78", "VFBGA", 2200),
    }
    assert mcp_decoys == {"DEV-L218-PREFIX-DECOY": ("L-218K8H-A", 999)}
    assert {
        row["DEVICE"] for row in results["production_0701"] if row["DEVICE"] in expected_rg_decoys
    } == expected_rg_decoys
    assert {
        row["DEVICE"] for row in results["wip_0701"] if row["DEVICE"] in expected_rg_decoys
    } == expected_rg_decoys


def test_langflow_dummy_fixture_has_six_rank_products_and_two_snapshot_times():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {
                        "dataset_key": "production",
                        "source_alias": "rank_input",
                        "required_params": {"DATE": "20260624"},
                    },
                    {
                        "dataset_key": "wip",
                        "source_alias": "rank_wip",
                        "required_params": {"DATE": "20260624"},
                    },
                    {
                        "dataset_key": "wip",
                        "source_alias": "snapshot_wip",
                        "required_params": {"DATE": "20260630"},
                    },
                ],
            }
        }
    )
    results = {item["source_alias"]: item["rows"] for item in payload["source_results"]}

    rank_input = {
        row["DEVICE"]: row["PRODUCTION"]
        for row in results["rank_input"]
        if row["DEVICE"].startswith("DEV-RANK-") and row["OPER_NAME"] == "INPUT"
    }
    rank_wip: dict[str, int] = {}
    for row in results["rank_wip"]:
        if row["DEVICE"].startswith("DEV-RANK-") and row["OPER_NAME"] in {"D/S1", "D/A1"}:
            rank_wip[row["DEVICE"]] = rank_wip.get(row["DEVICE"], 0) + row["WIP"]
    snapshots = {
        row["SNAPSHOT_TIME"]: row["WIP"]
        for row in results["snapshot_wip"]
        if row["DEVICE"] == "DEV-DA-GDDR6" and row["OPER_NAME"] == "D/A1"
    }

    assert rank_input == {f"DEV-RANK-{number}": 100 for number in range(1, 7)}
    assert rank_wip == {
        "DEV-RANK-1": 1200,
        "DEV-RANK-2": 1000,
        "DEV-RANK-3": 800,
        "DEV-RANK-4": 600,
        "DEV-RANK-5": 400,
        "DEV-RANK-6": 200,
    }
    assert snapshots == {"07:00": 224, "12:00": 999}


def test_langflow_dummy_fixture_has_auxiliary_multirow_join_controls():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    payload = dummy.retrieve_dummy_data(
        {
            "retrieval_job_bundle": {
                "source_type": "dummy",
                "jobs": [
                    {
                        "dataset_key": "eqp_uph",
                        "source_alias": "eqp_uph",
                        "required_params": {"DATE": "20260701"},
                    },
                    {
                        "dataset_key": "lot_status",
                        "source_alias": "lot_status",
                        "required_params": {},
                    },
                    {
                        "dataset_key": "hold_history",
                        "source_alias": "hold_history",
                        "required_params": {"LOT_ID": "T1234567GEN1"},
                    },
                ],
            }
        }
    )
    results = {item["source_alias"]: item["rows"] for item in payload["source_results"]}

    hbm_uph = {
        row["RECIPE_ID"]: row["UPH"]
        for row in results["eqp_uph"]
        if row["EQP_MODEL"] == "EQM-HBM"
    }
    dev002_lots = {
        row["LOT_ID"]: row["LOT_STAT"]
        for row in results["lot_status"]
        if row["DEVICE"] == "DEV002"
    }
    hold_history = [
        (row["HOLD_TM"], row["HOLD_CD"], row["HOLD_DESC"])
        for row in results["hold_history"]
    ]

    assert hbm_uph == {"RCP-002": 88.2, "RCP-007": 88.2, "RCP-HBM-ALT": 101.8}
    assert dev002_lots == {"T2222222GEN1": "WAITING", "T2222223GEN1": "RUNNING"}
    assert hold_history == [
        ("2026-06-30 18:00:00", "H000", "검증용 이전 HOLD 이력"),
        ("2026-07-01 08:00:00", "H001", "검증용 HOLD 이력"),
    ]


def test_data_analysis_split_mongodb_metadata_loaders_use_standalone_v4_node_inputs(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {
            "domain:process_groups:DA": {"_id": "domain:process_groups:DA", "section": "process_groups", "key": "DA", "status": "active", "payload": {"processes": ["D/A1"]}},
        },
        "agent_v4_table_catalog_items": {
            "table_catalog:wip_today": {"_id": "table_catalog:wip_today", "dataset_key": "wip_today", "status": "active", "payload": {"source_type": "oracle"}},
        },
        "agent_v4_main_flow_filters": {
            "main_flow_filter:DATE": {"_id": "main_flow_filter:DATE", "filter_key": "DATE", "status": "active", "payload": {"operator": "eq"}},
        },
    }
    domain_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01a_mongodb_domain_metadata_loader.py")
    table_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01b_mongodb_table_catalog_loader.py")
    main_variable_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01c_mongodb_main_variable_loader.py")
    candidates_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")

    domain_result = domain_loader.load_domain_metadata(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_domain_items",
        limit="50",
    )
    table_result = table_loader.load_table_catalog_metadata(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_table_catalog_items",
        limit="50",
    )
    main_variable_result = main_variable_loader.load_main_variable_metadata(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_main_flow_filters",
        limit="50",
    )
    result = candidates_builder.build_metadata_candidates(domain_result, table_result, main_variable_result)

    assert result["metadata_load"]["status"] == "ok"
    assert result["metadata_load"]["counts"] == {"domain_items": 1, "table_catalog_items": 1, "main_flow_filters": 1}
    assert result["metadata_load"]["loads"]["domain_items"]["database"] == "datagov"
    assert result["metadata_load"]["loads"]["domain_items"]["collection_name"] == "agent_v4_domain_items"
    assert result["metadata_load"]["loads"]["table_catalog_items"]["collection_name"] == "agent_v4_table_catalog_items"
    assert result["metadata_load"]["loads"]["main_flow_filters"]["collection_name"] == "agent_v4_main_flow_filters"
    assert result["metadata_load"]["loads"]["domain_items"]["status_filter"] == "active"
    assert result["metadata_candidates"]["table_catalog_items"][0]["dataset_key"] == "wip_today"
    assert result["metadata_candidates"]["domain_items"] == []


def test_metadata_candidates_remove_authoring_trace_but_keep_runtime_catalog_fields():
    candidates_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    result = candidates_builder.build_metadata_candidates(
        {"request": {"question": "DA production 메타데이터"}},
        {
            "domain_items": [
                {
                    "_id": "domain:process_groups:DA",
                    "section": "process_groups",
                    "key": "DA",
                    "text": "authoring-only text",
                    "registration_trace": {"raw_text": "원문 전체"},
                    "raw_trace": {"llm": "debug"},
                    "payload": {
                        "display_name": "D/A",
                        "aliases": ["DA"],
                        "raw_text": "payload raw text",
                        "description": "공정 그룹 설명",
                    },
                    "review": {"ready_to_save": True},
                    "updated_at": "2026-07-03T00:00:00Z",
                }
            ],
            "metadata_load": {"status": "ok"},
        },
        {
            "table_catalog_items": [
                {
                    "_id": "table_catalog:production_today",
                    "dataset_key": "production_today",
                    "registration_trace": {"raw_text": "catalog 원문"},
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {
                            "db_key": "PNT_RPT",
                            "query_template": "SELECT * FROM PROD WHERE WORK_DATE = {DATE}",
                        },
                        "required_params": ["DATE"],
                    },
                }
            ],
            "metadata_load": {"status": "ok"},
        },
        {"main_flow_filters": [], "metadata_load": {"status": "ok"}},
    )

    domain_item = result["metadata_candidates"]["domain_items"][0]
    catalog_item = result["metadata_candidates"]["table_catalog_items"][0]

    assert "_id" not in domain_item
    assert "text" not in domain_item
    assert "registration_trace" not in domain_item
    assert "raw_trace" not in domain_item
    assert "review" not in domain_item
    assert "updated_at" not in domain_item
    assert "raw_text" not in domain_item["payload"]
    assert domain_item["payload"]["description"] == "공정 그룹 설명"
    assert "registration_trace" not in catalog_item
    assert "query_template" not in catalog_item["payload"].get("source_config", {})
    assert "domain_items" not in result


def test_metadata_candidates_mark_non_runtime_pandas_function_cases():
    candidates_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    result = candidates_builder.build_metadata_candidates(
        {
            "request": {
                "question": "calculate_production_by_oper_name product_token_match component_token_product_lookup"
            }
        },
        {
            "domain_items": [
                {
                    "section": "pandas_function_cases",
                    "key": "calculate_production_by_oper_name",
                    "payload": {"function_name": "calculate_production_by_oper_name"},
                },
                {
                    "section": "pandas_function_cases",
                    "key": "product_token_match",
                    "payload": {"function_name": "match_product_tokens"},
                },
                {
                    "section": "pandas_function_cases",
                    "key": "component_token_product_lookup",
                    "payload": {"pseudocode": "filtered_df = match_product_tokens(product_dataframe, product_tokens)"},
                },
            ],
            "metadata_load": {"status": "ok"},
        },
        {"table_catalog_items": [], "metadata_load": {"status": "ok"}},
        {"main_flow_filters": [], "metadata_load": {"status": "ok"}},
    )

    items = {item["key"]: item for item in result["metadata_candidates"]["domain_items"]}
    assert items["calculate_production_by_oper_name"]["runtime_helper"] == {
        "function_name": "calculate_production_by_oper_name",
        "available": False,
        "selectable_for_intent": False,
        "selection_policy": "not_registered_runtime_helper",
    }
    assert "intent_plan.pandas_function_cases로 선택하지 않는다" in items["calculate_production_by_oper_name"]["selection_note"]
    assert items["product_token_match"]["runtime_helper"]["function_name"] == "match_product_tokens"
    assert items["product_token_match"]["runtime_helper"]["available"] is True
    assert items["product_token_match"]["runtime_helper"]["selectable_for_intent"] is True
    assert items["component_token_product_lookup"]["runtime_helper"]["function_name"] == "match_product_tokens"
    assert items["component_token_product_lookup"]["runtime_helper"]["selectable_for_intent"] is True
    assert result["metadata_candidates"]["runtime_function_helpers"][0]["function_name"] == "match_product_tokens"
    assert result["metadata_load"]["counts"] == {"domain_items": 3, "table_catalog_items": 0, "main_flow_filters": 0}


def test_data_analysis_mongodb_result_store_and_loader_round_trip(monkeypatch):
    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    result_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py")
    payload = {
        "request": {"session_id": "s1", "question": "재공 합계"},
        "metadata_refs": [{"type": "table_catalog", "key": "wip_today"}],
        "intent_plan": {"analysis_kind": "wip_sum"},
        "source_results": [{"source_alias": "wip_data", "row_count": 1}],
        "runtime_sources": {"wip_data": [{"OPER_NAME": "D/A1", "WIP": 120}]},
        "analysis": {"status": "ok", "row_count": 1, "columns": ["OPER_NAME", "wip_sum"], "rows": [{"OPER_NAME": "D/A1", "wip_sum": 120}]},
        "data": {"columns": ["OPER_NAME", "wip_sum"], "rows": [{"OPER_NAME": "D/A1", "wip_sum": 120}], "row_count": 1},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    stored = result_store.store_result(
        payload,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
        ttl_hours="3",
    )
    data_ref = stored["data"]["data_ref"]
    ref_id = data_ref["ref_id"]
    restored = result_loader.load_previous_result(
        {
            "request": {"session_id": "s1", "question": "이전 결과 다시 보여줘"},
            "state": {"current_data": {"data_ref": data_ref}},
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
    )

    assert ref_id.startswith("result:s1:")
    assert data_ref["role"] == "analysis_result"
    assert data_ref["path"] == "payload.result_rows"
    assert stored["data_refs"][0] == data_ref
    assert stored["data_refs"][1]["role"] == "source_rows"
    assert stored["data_refs"][1]["source_alias"] == "wip_data"
    assert stored["trace"]["inspection"]["result_store"]["collection_name"] == "agent_v4_result_store"
    assert stored["trace"]["inspection"]["result_store"]["ttl_hours"] == 3
    assert "expires_at" in stored["trace"]["inspection"]["result_store"]
    result_doc = mongo_store["datagov"]["agent_v4_result_store"][ref_id]
    assert result_doc["ttl_hours"] == 3
    assert isinstance(result_doc["expires_at"], datetime)
    assert result_doc["expires_at"] > datetime.now(timezone.utc)
    assert result_doc["payload"]["result_rows"] == [{"OPER_NAME": "D/A1", "wip_sum": 120}]
    assert "rows" not in result_doc["payload"]["data"]
    assert "rows" not in result_doc["payload"]["analysis"]
    assert restored["trace"]["inspection"]["result_loader"]["status"] == "ok"
    assert restored["runtime_sources"]["wip_data"][0]["WIP"] == 120
    assert restored["data"]["rows"] == [{"OPER_NAME": "D/A1", "wip_sum": 120}]
    assert restored["data"]["data_ref"] == data_ref

    data_ref_store = load_module(ROOT / "web_app" / "data_ref_store.py")
    result_rows = data_ref_store.load_data_ref_rows(data_ref, "mongodb://fake")
    source_rows = data_ref_store.load_data_ref_rows(stored["data_refs"][1], "mongodb://fake")
    assert result_rows["rows"] == [{"OPER_NAME": "D/A1", "wip_sum": 120}]
    assert source_rows["rows"] == [{"OPER_NAME": "D/A1", "WIP": 120}]


def test_mongodb_result_loader_accepts_legacy_data_rows(monkeypatch):
    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py")
    mongo_store.setdefault("datagov", {}).setdefault("agent_v4_result_store", {})["legacy-ref"] = {
        "_id": "legacy-ref",
        "payload": {
            "source_results": [],
            "runtime_sources": {},
            "analysis": {"status": "ok", "row_count": 1},
            "data": {"columns": ["DEVICE"], "rows": [{"DEVICE": "LEGACY"}], "row_count": 1},
        },
    }

    restored = result_loader.load_previous_result(
        {
            "data": {"data_ref": "legacy-ref"},
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
    )

    assert restored["trace"]["inspection"]["result_loader"]["status"] == "ok"
    assert restored["data"]["rows"] == [{"DEVICE": "LEGACY"}]
    assert restored["data"]["data_ref"]["path"] == "payload.result_rows"


def test_data_analysis_mongodb_result_store_has_ttl_input():
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")

    input_names = {item.kwargs.get("name") for item in result_store.MongoDBResultStore.inputs}

    assert "ttl_hours" in input_names
    assert {"max_result_rows", "max_source_rows_per_alias", "max_document_bytes"} <= input_names


def test_result_store_fails_closed_instead_of_exposing_truncated_followup_ref(monkeypatch):
    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    rows = [{"DEVICE": "DEV-A", "QTY": 1}, {"DEVICE": "DEV-B", "QTY": 2}]
    payload = {
        "request": {"session_id": "s-limit", "question": "전체 결과"},
        "runtime_sources": {"production_data": rows},
        "source_results": [{"source_alias": "production_data", "row_count": 2}],
        "analysis": {"status": "ok", "row_count": 2, "columns": ["DEVICE", "QTY"]},
        "data": {"columns": ["DEVICE", "QTY"], "rows": rows, "row_count": 2},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    stored = result_store.store_result(
        payload,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_result_store",
        max_result_rows="1",
    )

    assert stored["trace"]["inspection"]["result_store"]["status"] == "followup_unavailable"
    assert stored["trace"]["inspection"]["result_store"]["data_ref"] == ""
    assert stored["trace"]["inspection"]["result_store"]["storage_manifest"]["result_rows"] == {
        "original_count": 2,
        "stored_count": 1,
        "complete": False,
    }
    assert "data_ref" not in stored["data"]
    assert mongo_store.get("datagov", {}).get("agent_v4_result_store", {}) == {}


def test_result_store_skips_mongodb_after_required_retrieval_failure(monkeypatch):
    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    payload = {
        "execution_gate": {"status": "blocked"},
        "analysis": {"status": "error"},
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    stored = result_store.store_result(payload)

    assert stored["trace"]["inspection"]["result_store"]["status"] == "skipped"
    assert stored["trace"]["inspection"]["result_store"]["reason"] == "required_source_retrieval_failed"
    assert mongo_store == {}


def test_answer_builder_preserves_deterministic_gate_error_when_answer_agent_is_skipped():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    payload = {
        "execution_gate": {"status": "blocked"},
        "answer_message": "필수 데이터 조회에 실패하여 pandas 분석과 답변 LLM을 실행하지 않았습니다.",
        "analysis": {"status": "error"},
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    answered = answer_builder.build_answer_response(payload, "")

    assert answered["answer_message"] == payload["answer_message"]
    assert answered["answer_sections"]["summary"]["headline"] == payload["answer_message"]


def test_required_retrieval_failure_short_circuits_to_single_deterministic_api_response(monkeypatch):
    mongo_store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    pandas_executor = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "17_pandas_code_executor.py")
    result_store = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py")
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    repair_calls = []
    payload = {
        "request": {"retrieval_mode": "live", "question": "생산량 알려줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_data", "source_type": "oracle"}
            ]
        },
        "source_results": [
            {
                "dataset_key": "production",
                "source_alias": "production_data",
                "source_type": "oracle",
                "status": "error",
                "errors": [{"type": "timeout", "message": "Oracle timeout"}],
            }
        ],
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    gated = gate.apply_retrieval_execution_gate(payload)
    executed = pandas_executor.execute_pandas_with_repair(
        gated,
        {"code": "raise RuntimeError('must not execute')"},
        repair_invoker=lambda prompt: repair_calls.append(prompt),
    )
    stored = result_store.store_result(executed)
    answered = answer_builder.build_answer_response(stored, "")
    response = api_builder.build_api_response(answered)

    assert repair_calls == []
    assert mongo_store == {}
    assert response["status"] == "error"
    assert response["stage_status"] == {"overall": "error", "retrieval": "error", "analysis": "error"}
    assert response["message"] == gated["answer_message"]
    assert response["trace"]["inspection"]["pandas_execution"]["status"] == "skipped"
    assert response["trace"]["inspection"]["result_store"]["status"] == "skipped"


def test_data_ref_store_rejects_expired_document():
    data_ref_store = load_module(ROOT / "web_app" / "data_ref_store.py")

    loaded = data_ref_store.rows_from_data_ref_document(
        {
            "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "payload": {"result_rows": [{"DEVICE": "DEV-A"}]},
        },
        path="payload.result_rows",
    )

    assert loaded["ok"] is False
    assert loaded["expired"] is True
    assert loaded["rows"] == []


def test_mongodb_previous_result_loader_uses_payload_data_ref_only():
    result_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py")

    input_names = {item.kwargs.get("name") for item in result_loader.MongoDBResultLoader.inputs}
    payload = {"trace": {"warnings": [], "errors": [], "inspection": {}}}
    skipped = result_loader.load_previous_result(payload)

    assert "payload" in input_names
    assert "data_ref" not in input_names
    assert skipped["trace"]["warnings"] == []
    assert skipped["trace"]["inspection"]["result_loader"]["status"] == "skipped"
    assert skipped["trace"]["inspection"]["result_loader"]["errors"][0]["type"] == "missing_data_ref"


def test_restored_runtime_sources_survive_empty_retrieval_merge():
    merger = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "13_source_retrieval_merger.py")
    adapter = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14_retrieval_payload_adapter.py")
    payload = {
        "source_results": [{"source_alias": "wip_data", "row_count": 1}],
        "runtime_sources": {"wip_data": [{"OPER_NAME": "D/A1", "WIP": 120}]},
        "trace": {"warnings": [], "errors": [], "inspection": {"result_loader": {"status": "ok"}}},
    }

    merged = merger.merge_source_retrieval_payloads(payload, {"source_type": "oracle", "status": "skipped", "skipped": True, "skip_reason": "no oracle retrieval jobs"})
    adapted = adapter.build_retrieval_payload(merged)

    assert adapted["runtime_sources"]["wip_data"][0]["WIP"] == 120
    assert adapted["trace"]["inspection"]["data_retrieval"]["preserved_existing_runtime_sources"] is True


def test_oracle_retriever_executes_sql_with_configured_tns():
    oracle = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "09_oracle_query_retriever.py")

    class FakeCursor:
        description = [("WORK_DATE",), ("PRODUCTION",)]

        def __init__(self):
            self.executed_sql = ""

        def execute(self, sql):
            self.executed_sql = sql

        def fetchmany(self, limit):
            assert limit == 100
            return [("20260701", 1234)]

        def close(self):
            pass

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def close(self):
            pass

    class FakeOracleModule:
        def __init__(self):
            self.connection = FakeConnection()
            self.connect_kwargs = {}

        def connect(self, **kwargs):
            self.connect_kwargs = kwargs
            return self.connection

    fake_oracle = FakeOracleModule()
    oracle.OracleQueryRetriever.oracledb = fake_oracle
    payload = {
        "retrieval_job_bundle": {
            "source_type": "oracle",
            "jobs": [
                {
                    "job_id": "job_1",
                    "dataset_key": "production_today",
                    "source_alias": "prod_data",
                    "source_type": "oracle",
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "PNT_RPT",
                        "query_template": "SELECT WORK_DATE, PRODUCTION FROM PROD_TABLE WHERE WORK_DATE = {DATE}",
                    },
                    "required_params": {"DATE": "20260701"},
                    "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
                }
            ],
        }
    }

    result = oracle.retrieve_oracle_data(payload, json.dumps({"PNT_RPT": {"user": "u", "password": "p", "tns": "tns-value"}}), "100")
    source_result = result["source_results"][0]

    assert result["status"] == "ok"
    assert fake_oracle.connect_kwargs == {"user": "u", "password": "p", "dsn": "tns-value"}
    assert source_result["rows"] == [{"WORK_DATE": "20260701", "PRODUCTION": 1234}]
    assert source_result["source_execution"]["executed_query"] == "SELECT WORK_DATE, PRODUCTION FROM PROD_TABLE WHERE WORK_DATE = '20260701'"
    assert source_result["source_execution"]["filters_applied_in_retriever"] is False
    assert source_result["pandas_filters"] == {"OPER_NAME": {"operator": "eq", "value": "D/A1"}}
    assert "applied_filters" not in source_result


def test_oracle_retriever_preserves_columns_when_query_returns_no_rows():
    oracle = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "09_oracle_query_retriever.py")

    class FakeCursor:
        description = [("WORK_DATE",), ("TECH",), ("PRODUCTION",)]

        def execute(self, sql):
            self.executed_sql = sql

        def fetchmany(self, limit):
            return []

        def close(self):
            pass

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    class FakeOracleModule:
        def connect(self, **kwargs):
            return FakeConnection()

    oracle.OracleQueryRetriever.oracledb = FakeOracleModule()
    payload = {
        "retrieval_job_bundle": {
            "source_type": "oracle",
            "jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "production_data",
                    "source_type": "oracle",
                    "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT WORK_DATE, TECH, PRODUCTION FROM PROD WHERE WORK_DATE = {DATE}"},
                    "required_params": {"DATE": "20260706"},
                }
            ],
        }
    }

    result = oracle.retrieve_oracle_data(payload, json.dumps({"PNT_RPT": {"tns": "tns-value"}}), "100")
    source_result = result["source_results"][0]

    assert result["status"] == "ok"
    assert source_result["row_count"] == 0
    assert source_result["columns"] == ["WORK_DATE", "TECH", "PRODUCTION"]


def test_oracle_retriever_parses_named_tns_block():
    oracle = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "09_oracle_query_retriever.py")

    config, errors = oracle._oracle_config_from_value("PNT_RPT:\n(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP))(CONNECT_DATA=(SERVICE_NAME=PNT)))")

    assert errors == []
    assert config == {"PNT_RPT": {"tns": "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP))(CONNECT_DATA=(SERVICE_NAME=PNT)))"}}


def test_langflow_prompt_templates_are_external_files_for_agent_nodes():
    prompt_files = [
        ROOT / "langflow_components" / "data_analysis_flow" / "03_intent_prompt_template_ko.md",
        ROOT / "langflow_components" / "data_analysis_flow" / "16_pandas_prompt_template_ko.md",
        ROOT / "langflow_components" / "data_analysis_flow" / "19_answer_prompt_template_ko.md",
        ROOT / "langflow_components" / "domain_saving_flow" / "03_saving_prompt_template_ko.md",
        ROOT / "langflow_components" / "table_catalog_saving_flow" / "03_saving_prompt_template_ko.md",
        ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "03_saving_prompt_template_ko.md",
    ]
    for path in prompt_files:
        text = path.read_text(encoding="utf-8")
        assert "너는" in text

    for guide_name in [
        "data_analysis_flow",
        "domain_saving_flow",
        "table_catalog_saving_flow",
        "main_flow_filters_saving_flow",
    ]:
        guide = (ROOT / "langflow_components" / guide_name / "CONNECTION_GUIDE.md").read_text(encoding="utf-8")
        assert "Langflow Prompt Template" in guide
        assert "Langflow Agent/LLM" in guide


def test_metadata_saving_guide_uses_current_writer_ports():
    guide = (ROOT / "docs" / "METADATA_SAVING_FLOW_GUIDE.md").read_text(encoding="utf-8")

    assert "MongoDB Writer.authoring_payload" not in guide
    assert "MongoDB Writer.review_payload" not in guide
    assert "Review Writer.review_response" not in guide
    assert "Existing Items Loader.existing_items" in guide
    assert "Similarity Checker.existing_items" in guide
    assert "Response Normalizer.payload" in guide
    assert "Message Adapter.message" in guide
    assert "API Response Builder.display_message" in guide


def test_data_analysis_connection_guide_covers_v5_critical_boundaries():
    guide = (ROOT / "langflow_components" / "data_analysis_flow" / "CONNECTION_GUIDE.md").read_text(encoding="utf-8")

    assert "01D 질문 기반 메타데이터 후보 생성기" in guide
    assert "04A 신뢰 카탈로그 조회 작업 구성기" in guide
    assert "15A 선택 helper 코드 생성기" in guide
    assert "단일 pandas 실행 노드와 오류 시 1회 복구 경로" in guide
    assert "첫 pandas LLM이 생성한 원본 코드 전체" in guide
    assert "오류 유형·메시지·축약 traceback" in guide
    assert "전체 payload를 분기하지 않습니다" in guide
    assert "세션 writer는 최종 출력과 병렬로 연결하지 않습니다" in guide


def test_langflow_prompt_templates_only_expose_valid_variables():
    import re

    allowed = {
            "raw_text",
            "source_text",
        "existing_metadata_summary",
        "refined_text",
        "review_input_json",
        "question",
        "state_summary",
        "metadata_candidates",
        "specialized_prompt",
        "output_schema",
        "intent_plan_json",
        "source_schema_json",
        "source_preview_json",
        "function_case_helper_code",
        "function_case_selection_json",
        "repair_required",
        "failed_code",
        "error_context_json",
            "output_contract_json",
            "result_summary_json",
            "applied_scope_json",
            "answer_context_json",
            "metadata_context_json",
                "domain_answer_guidance",
                "warnings_errors_json",
                "user_input",
            "route_candidates_json",
            "routing_rules",
            "output_schema_json",
        }
    prompt_files = sorted((ROOT / "langflow_components").glob("*/*prompt_template_ko.md"))
    for path in prompt_files:
        text = path.read_text(encoding="utf-8")
        variables = {
            match.group(1)
            for match in re.finditer(r"(?<!\{)\{([^{}\r\n]+)\}(?!\})", text)
        }
        assert variables <= allowed, f"{path.name} exposes invalid Langflow variables: {variables - allowed}"
        assert "" not in variables


def test_langflow_prompt_templates_keep_domain_specific_examples_out_of_generic_prompts():
    prompt_text_by_path = {
        path: path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "langflow_components").glob("*/*prompt_template_ko.md"))
    }
    specialized_prompt = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "specialized_prompt_input_example_ko.md"
    ).read_text(encoding="utf-8")
    moved_to_specialized_prompt_terms = [
        "match_product_tokens",
        "sample_passthrough_helper",
        "RG 32G DDR4 FBGA 96 DDP",
        "DA 16G GDDR6 180",
        "PKG OUT",
        "BOH",
        "현시간 기준 재공",
        "x16",
        "X8",
        "L-218",
        "A-663",
    ]
    generic_prompt_blocklist = moved_to_specialized_prompt_terms + [
        "제품 token 매칭용",
        "일반 pandas filter로 표현 가능해 보여도",
        "MCP_NO",
        "POP",
        "MOBILE",
        "HBM",
        "D/A",
        "wip_today",
        "PNT_RPT",
    ]

    for term in generic_prompt_blocklist:
        for path, text in prompt_text_by_path.items():
            assert term not in text, f"{path.name} contains domain-specific example: {term}"

    for term in moved_to_specialized_prompt_terms:
        assert term in specialized_prompt
    assert "단일 token" in specialized_prompt
    assert "L-123 제품 생산량" in specialized_prompt
    assert "영문 1자리-숫자 3자리(+선택 영숫자) 패턴의 token은 값이 무엇이든" in specialized_prompt
    assert "A-663 제품" in specialized_prompt
    assert "B-123C1제품" in specialized_prompt
    assert "Q-555A9 제품 재공" in specialized_prompt
    assert "DEVICE filter로 만들지 않는다" in specialized_prompt
    assert "input_text에는 제품이라는 말을 빼고 패턴 token만 남긴다" in specialized_prompt
    assert "일반 pandas filter로 표현 가능해 보여도" in specialized_prompt
    assert "등록된 제품군" in specialized_prompt
    assert "152ball" in specialized_prompt
    assert "78Lead" in specialized_prompt
    assert "제품별과 DEVICE" in specialized_prompt
    assert "DEVICE만 단독으로 보여주지 않는다" in specialized_prompt


def test_pandas_prompt_templates_do_not_repeat_executor_filter_preamble():
    generation_prompt = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "16_pandas_prompt_template_ko.md"
    ).read_text(encoding="utf-8")
    repair_prompt = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "17b_pandas_repair_prompt_template_ko.md"
    ).read_text(encoding="utf-8")

    assert "executor가 pandas filter preamble으로 자동 적용한다" in generation_prompt
    assert "같은 조건을 다시 작성하지 않는다" in generation_prompt
    assert "동일한 필터를 반복 적용하면" in generation_prompt
    assert "retry code에는 `intent_plan.retrieval_jobs[].filters`와 같은 필터를 다시 작성하지 않는다" in repair_prompt
    assert "같은 필터를 코드 안에서 반복해도" not in generation_prompt


def test_intent_prompt_requires_specific_current_analysis_kind():
    prompt = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "03_intent_prompt_template_ko.md"
    ).read_text(encoding="utf-8")

    assert "`analysis_kind`" in prompt
    assert "snake_case" in prompt
    assert "retrieval_jobs" in prompt and "metric" in prompt
    assert "production_analysis" in prompt
    assert "target_analysis" in prompt
    assert "target_plan_by_product" in prompt
    assert "이전 `analysis_kind`를 그대로 상속하지" in prompt
    assert "`INPUT 계획`, `OUT 계획`" in prompt
    assert "실제/실적과의 비교" in prompt
    assert "production dataset" in prompt
    assert "`OPER_NAME=INPUT`" in prompt
    assert "추가하지 않는다" in prompt


def test_pandas_prompt_templates_preserve_yyyymmdd_date_columns():
    prompt_dir = ROOT / "langflow_components" / "data_analysis_flow"
    for filename in (
        "16_pandas_prompt_template_ko.md",
        "17b_pandas_repair_prompt_template_ko.md",
    ):
        text = (prompt_dir / filename).read_text(encoding="utf-8")

        assert "WORK_DT" in text, filename
        assert "WORK_DATE" in text, filename
        assert "YYYYMMDD" in text, filename
        assert "숫자" in text and "변환하지" in text, filename
        assert "보존" in text, filename
        assert "pd.to_datetime" in text, filename
        assert "임시" in text, filename


def test_pandas_prompts_document_exact_safe_import_compatibility_and_pandas_alternatives():
    prompt_dir = ROOT / "langflow_components" / "data_analysis_flow"
    for filename in (
        "16_pandas_prompt_template_ko.md",
        "17b_pandas_repair_prompt_template_ko.md",
    ):
        text = (prompt_dir / filename).read_text(encoding="utf-8")

        assert "namespace" in text and "np" in text and "numpy" in text, filename
        assert "import pandas as pd" in text, filename
        assert "import numpy as np" in text, filename
        assert "제한" in text and "파일 I/O" in text, filename
        assert "Series.where" in text and "mask" in text, filename
        assert "numerator.div(denominator)" in text, filename
        assert "denominator.ne(0)" in text, filename
        assert "fillna(0)" in text, filename
        assert "안전 builtin" in text and "`zip`" in text, filename
        assert "목록 밖 builtin" in text, filename


def test_prompt_variable_builder_output_order_matches_prompt_input_order():
    import re

    prompt_to_builder = [
        ("data_analysis_flow/03_intent_prompt_template_ko.md", "data_analysis_flow/02_intent_variables_builder.py"),
        ("data_analysis_flow/16_pandas_prompt_template_ko.md", "data_analysis_flow/15_pandas_variables_builder.py"),
        ("data_analysis_flow/19_answer_prompt_template_ko.md", "data_analysis_flow/18_answer_variables_builder.py"),
        ("domain_saving_flow/03_saving_prompt_template_ko.md", "domain_saving_flow/03_domain_saving_variables_builder.py"),
        ("table_catalog_saving_flow/03_saving_prompt_template_ko.md", "table_catalog_saving_flow/03_table_catalog_saving_variables_builder.py"),
        ("main_flow_filters_saving_flow/03_saving_prompt_template_ko.md", "main_flow_filters_saving_flow/03_main_flow_filter_saving_variables_builder.py"),
    ]
    manual_prompt_variables = {
        "data_analysis_flow/03_intent_prompt_template_ko.md": {"specialized_prompt"},
            "data_analysis_flow/16_pandas_prompt_template_ko.md": {"function_case_helper_code"},
            "data_analysis_flow/19_answer_prompt_template_ko.md": {"domain_answer_guidance"},
        }

    for prompt_relpath, builder_relpath in prompt_to_builder:
        prompt_path = ROOT / "langflow_components" / prompt_relpath
        builder_path = ROOT / "langflow_components" / builder_relpath
        prompt_variables = []
        for match in re.finditer(r"(?<!\{)\{([^{}\r\n]+)\}(?!\})", prompt_path.read_text(encoding="utf-8")):
            variable_name = match.group(1)
            if variable_name not in prompt_variables:
                prompt_variables.append(variable_name)

        module = load_module(builder_path)
        component_classes = [
            value
            for value in vars(module).values()
            if isinstance(value, type) and value.__module__ == module.__name__ and hasattr(value, "outputs")
        ]
        assert len(component_classes) == 1, builder_path.name
        output_names = [item.kwargs.get("name") for item in component_classes[0].outputs]

        expected_output_names = [
            name
            for name in prompt_variables
            if name not in manual_prompt_variables.get(prompt_relpath, set())
        ]

        assert output_names == expected_output_names, f"{builder_path.name} output order must match {prompt_path.name} input order"


def test_variable_builders_do_not_expose_redundant_variables_output():
    variable_builder_files = sorted((ROOT / "langflow_components").glob("*/*variables_builder.py"))
    assert variable_builder_files
    for path in variable_builder_files:
        text = path.read_text(encoding="utf-8")
        assert 'name="variables"' not in text, f"{path.name} exposes redundant variables output"
        assert 'display_name="변수"' not in text, f"{path.name} exposes redundant variables display label"
        assert "def build_payload" not in text, f"{path.name} keeps redundant variables payload builder"


def test_multi_output_components_expose_all_ports_simultaneously():
    for path in COMPONENT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "outputs" for target in node.targets):
                continue
            if not isinstance(node.value, ast.List):
                continue
            output_calls = [item for item in node.value.elts if isinstance(item, ast.Call)]
            if len(output_calls) <= 1:
                continue
            for call in output_calls:
                kwargs = {keyword.arg: keyword.value for keyword in call.keywords}
                group_outputs = kwargs.get("group_outputs")
                assert isinstance(group_outputs, ast.Constant) and group_outputs.value is True, f"{path.name}:{call.lineno} multi-output port is missing group_outputs=True"


def test_message_output_ports_declare_message_type():
    for path in COMPONENT_FILES:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        message_methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and isinstance(node.returns, ast.Name)
            and node.returns.id == "Message"
        }
        if not message_methods:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "Output":
                continue
            kwargs = {keyword.arg: keyword.value for keyword in node.keywords}
            method_value = kwargs.get("method")
            if not isinstance(method_value, ast.Constant) or method_value.value not in message_methods:
                continue
            types_value = kwargs.get("types")
            assert isinstance(types_value, ast.List), f"{path.name}:{node.lineno} Message output is missing types=['Message']"
            type_names = [item.value for item in types_value.elts if isinstance(item, ast.Constant)]
            assert "Message" in type_names, f"{path.name}:{node.lineno} Message output has wrong types={type_names}"


def test_domain_langflow_saving_blocks_source_config_in_dry_run():
    request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    payload = request_loader.build_request("BAD domain", "ask", "true")
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "BAD",
                    "payload": {"source_config": {"query_template": "SELECT * FROM X"}},
                }
            ]
        },
    )

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is False
    assert result["write_result"]["errors"][0]["type"] == "domain_source_config_forbidden"


def test_domain_writer_keeps_deterministic_blockers_even_when_review_is_ready():
    request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    payload = request_loader.build_request("BAD domain", "ask", "true")
    payload = normalizer.normalize_authoring(
        payload,
        {"items": [{"section": "process_groups", "key": "BAD", "payload": {"source_config": {"query_template": "SELECT * FROM X"}}}]},
    )

    result = writer.review_and_write(payload, {"ready_to_save": True, "errors": [], "supplement_requests": []})

    assert result["write_result"]["success"] is False
    assert result["write_result"]["errors"][0]["type"] == "domain_source_config_forbidden"


def test_metadata_writers_preserve_refinement_and_fail_closed_on_missing_information():
    specs = [
        (
            "domain_saving_flow/00_domain_saving_request_loader.py",
            "domain_saving_flow/04_domain_saving_result_normalizer.py",
            "domain_saving_flow/07_domain_review_writer.py",
            "domain_saving_flow/08_domain_saving_response_builder.py",
            {"items": [{"section": "process_groups", "key": "DA", "payload": {"display_name": "D/A"}}]},
        ),
        (
            "table_catalog_saving_flow/00_table_catalog_saving_request_loader.py",
            "table_catalog_saving_flow/04_table_catalog_saving_result_normalizer.py",
            "table_catalog_saving_flow/07_table_catalog_review_writer.py",
            "table_catalog_saving_flow/08_table_catalog_saving_response_builder.py",
            {"items": [{"dataset_key": "wip_today", "payload": {"source_type": "oracle", "source_config": {"source_type": "oracle", "query_template": "SELECT 1"}}}]},
        ),
        (
            "main_flow_filters_saving_flow/00_main_flow_filter_saving_request_loader.py",
            "main_flow_filters_saving_flow/04_main_flow_filter_saving_result_normalizer.py",
            "main_flow_filters_saving_flow/07_main_flow_filter_review_writer.py",
            "main_flow_filters_saving_flow/08_main_flow_filter_saving_response_builder.py",
            {"items": [{"filter_key": "DATE", "payload": {"display_name": "기준일", "operator": "eq", "value_type": "date", "value_shape": "scalar"}}]},
        ),
    ]
    for request_path, normalizer_path, writer_path, response_path, llm_result in specs:
        request_loader = load_module(ROOT / "langflow_components" / request_path)
        normalizer = load_module(ROOT / "langflow_components" / normalizer_path)
        writer = load_module(ROOT / "langflow_components" / writer_path)
        response_builder = load_module(ROOT / "langflow_components" / response_path)
        payload = request_loader.build_request("정보가 덜 들어간 등록 요청", "replace", False)
        llm_result = deepcopy(llm_result)
        llm_result["missing_information"] = ["저장 대상의 필수 설명을 추가해 주세요."]
        llm_result["assumptions"] = ["현재 입력만으로 source를 추정하지 않습니다."]

        normalized = normalizer.normalize_authoring(payload, llm_result)
        result = writer.review_and_write(normalized, {"ready_to_save": True, "errors": [], "supplement_requests": []})
        response = response_builder.build_response(result)

        assert normalized["refinement"]["needs_more_input"] is True
        assert normalized["refinement"]["missing_information"] == ["저장 대상의 필수 설명을 추가해 주세요."]
        assert normalized["refinement"]["assumptions"] == ["현재 입력만으로 source를 추정하지 않습니다."]
        assert result["review"]["ready_to_save"] is False
        assert result["review"]["assumptions"] == ["현재 입력만으로 source를 추정하지 않습니다."]
        assert result["write_result"]["status"] == "needs_input"
        assert result["write_result"]["saved_count"] == 0
        assert response["status"] == "needs_input"
        assert any(notice["title"] == "적용 가정" for notice in response["answer_sections"]["notices"])


def test_metadata_writers_block_unparseable_authoring_response():
    specs = [
        ("domain_saving_flow/00_domain_saving_request_loader.py", "domain_saving_flow/04_domain_saving_result_normalizer.py", "domain_saving_flow/07_domain_review_writer.py"),
        ("table_catalog_saving_flow/00_table_catalog_saving_request_loader.py", "table_catalog_saving_flow/04_table_catalog_saving_result_normalizer.py", "table_catalog_saving_flow/07_table_catalog_review_writer.py"),
        ("main_flow_filters_saving_flow/00_main_flow_filter_saving_request_loader.py", "main_flow_filters_saving_flow/04_main_flow_filter_saving_result_normalizer.py", "main_flow_filters_saving_flow/07_main_flow_filter_review_writer.py"),
    ]
    for request_path, normalizer_path, writer_path in specs:
        request_loader = load_module(ROOT / "langflow_components" / request_path)
        normalizer = load_module(ROOT / "langflow_components" / normalizer_path)
        writer = load_module(ROOT / "langflow_components" / writer_path)
        payload = request_loader.build_request("등록 요청", "replace", False)

        normalized = normalizer.normalize_authoring(payload, "not-json")
        result = writer.review_and_write(normalized, {"ready_to_save": True, "errors": [], "supplement_requests": []})

        assert any(error["type"] == "llm_response_parse_error" for error in normalized["errors"])
        assert result["write_result"]["success"] is False
        assert result["write_result"]["status"] == "error"


def test_table_catalog_langflow_writer_blocks_truncated_query():
    request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    payload = request_loader.build_request("bad query", "ask", "true")
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "dataset_key": "bad",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT ..."},
                    },
                }
            ]
        },
    )

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is False
    assert any(error["type"] == "truncated_query" for error in result["write_result"]["errors"])


def test_table_catalog_writer_allows_sql_line_comments_and_preserves_query():
    request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    sql = "--쿼리 작성\nSELECT WORK_DATE, OPER_NAME, WIP\nFROM WIP_TABLE\nWHERE WORK_DATE = {DATE}"
    payload = request_loader.build_request("commented query", "ask", "true")
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "dataset_key": "wip_today",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": sql},
                    },
                }
            ]
        },
    )

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is True
    assert result["items"][0]["payload"]["source_config"]["query_template"] == sql


def test_table_catalog_writer_allows_with_cte_query():
    request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    sql = "WITH base AS (\n  SELECT WORK_DATE, OPER_NAME, WIP FROM WIP_TABLE\n)\nSELECT * FROM base WHERE WORK_DATE = {DATE}"
    payload = request_loader.build_request("with query", "ask", "true")
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "dataset_key": "wip_today_cte",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": sql},
                    },
                }
            ]
        },
    )

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is True
    assert result["items"][0]["payload"]["source_config"]["query_template"].startswith("WITH base AS")


def test_table_and_filter_writers_respect_negative_review_response():
    table_request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    table_normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    table_writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    filter_request_loader = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "00_main_flow_filter_saving_request_loader.py")
    filter_normalizer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "04_main_flow_filter_saving_result_normalizer.py")
    filter_writer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "07_main_flow_filter_review_writer.py")
    table_payload = table_request_loader.build_request("wip_today", "ask", "true")
    table_payload = table_normalizer.normalize_authoring(
        table_payload,
        {
            "items": [
                {
                    "dataset_key": "wip_today",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT WORK_DATE, WIP FROM WIP_TABLE WHERE WORK_DATE = {DATE}"},
                    },
                }
            ]
        },
    )
    filter_payload = filter_request_loader.build_request("DATE는 기준일입니다.", "ask", "true")
    filter_payload = filter_normalizer.normalize_authoring(
        filter_payload,
        {"items": [{"filter_key": "DATE", "payload": {"display_name": "기준일", "aliases": ["오늘"], "operator": "eq", "value_type": "date", "value_shape": "scalar"}}]},
    )
    negative_review = {"ready_to_save": False, "errors": [{"type": "review_rejected", "message": "검수 보류"}], "supplement_requests": []}

    table_result = table_writer.review_and_write(table_payload, negative_review)
    filter_result = filter_writer.review_and_write(filter_payload, negative_review)

    assert table_result["write_result"]["success"] is False
    assert table_result["write_result"]["errors"][0]["type"] == "review_rejected"
    assert filter_result["write_result"]["success"] is False
    assert filter_result["write_result"]["errors"][0]["type"] == "review_rejected"


def test_authoring_writers_use_shared_v4_mongo_env_defaults(monkeypatch):
    setattr(builtins, "_metadata_driven_v5_qa_snapshot_cache_v1", {"generation": 0, "entries": {"stale": {}}})
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    domain_request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    domain_writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    table_request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    table_normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    table_writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    filter_request_loader = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "00_main_flow_filter_saving_request_loader.py")
    filter_normalizer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "04_main_flow_filter_saving_result_normalizer.py")
    filter_writer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "07_main_flow_filter_review_writer.py")

    domain_payload = domain_request_loader.build_request("DA는 D/A1 공정입니다.", "ask", "false")
    domain_payload = domain_normalizer.normalize_authoring(domain_payload, {"items": [{"section": "process_groups", "key": "DA", "payload": {"processes": ["D/A1"]}}]})
    table_payload = table_request_loader.build_request("wip_today", "ask", "false")
    table_payload = table_normalizer.normalize_authoring(
        table_payload,
        {
            "items": [
                {
                    "dataset_key": "wip_today",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT WORK_DATE, WIP FROM WIP_TABLE WHERE WORK_DATE = {DATE}"},
                    },
                }
            ]
        },
    )
    filter_payload = filter_request_loader.build_request("DATE는 기준일입니다.", "ask", "false")
    filter_payload = filter_normalizer.normalize_authoring(
        filter_payload,
        {"items": [{"filter_key": "DATE", "payload": {"display_name": "기준일", "aliases": ["오늘"], "operator": "eq", "value_type": "date", "value_shape": "scalar"}}]},
    )

    positive_review = {"ready_to_save": True, "errors": [], "supplement_requests": []}
    domain_result = domain_writer.review_and_write(domain_payload, positive_review)
    table_result = table_writer.review_and_write(table_payload, positive_review)
    filter_result = filter_writer.review_and_write(filter_payload, positive_review)

    assert domain_result["write_result"]["collection_name"] == "agent_v4_domain_items"
    assert table_result["write_result"]["collection_name"] == "agent_v4_table_catalog_items"
    assert filter_result["write_result"]["collection_name"] == "agent_v4_main_flow_filters"
    assert "domain:process_groups:DA" in store["datagov"]["agent_v4_domain_items"]
    assert "table_catalog:wip_today" in store["datagov"]["agent_v4_table_catalog_items"]
    assert "main_flow_filter:DATE" in store["datagov"]["agent_v4_main_flow_filters"]
    assert domain_result["write_result"]["metadata_qa_snapshot_invalidated"] is True
    assert table_result["write_result"]["metadata_qa_snapshot_invalidated"] is True
    assert filter_result["write_result"]["metadata_qa_snapshot_invalidated"] is True
    registry = getattr(builtins, "_metadata_driven_v5_qa_snapshot_cache_v1")
    assert registry["generation"] == 3
    assert registry["entries"] == {}


def test_authoring_existing_item_loaders_use_shared_v4_mongo_env_defaults(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {"domain:process_groups:DA": {"_id": "domain:process_groups:DA", "section": "process_groups", "key": "DA", "payload": {}}},
        "agent_v4_table_catalog_items": {"table_catalog:wip_today": {"_id": "table_catalog:wip_today", "dataset_key": "wip_today", "payload": {}}},
        "agent_v4_main_flow_filters": {"main_flow_filter:DATE": {"_id": "main_flow_filter:DATE", "filter_key": "DATE", "payload": {}}},
    }
    domain_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_existing_items_loader.py")
    table_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_existing_items_loader.py")
    filter_loader = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "00_main_flow_filter_existing_items_loader.py")

    domain_result = domain_loader.load_existing_items()
    table_result = table_loader.load_existing_items()
    filter_result = filter_loader.load_existing_items()

    assert domain_result["metadata_load"]["collection_name"] == "agent_v4_domain_items"
    assert table_result["metadata_load"]["collection_name"] == "agent_v4_table_catalog_items"
    assert filter_result["metadata_load"]["collection_name"] == "agent_v4_main_flow_filters"
    assert domain_result["existing_items"][0]["key"] == "DA"
    assert table_result["existing_items"][0]["dataset_key"] == "wip_today"
    assert filter_result["existing_items"][0]["filter_key"] == "DATE"


def test_mongodb_metadata_export_upload_round_trip(monkeypatch, tmp_path):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {
            "domain:process_groups:DA": {"_id": "domain:process_groups:DA", "section": "process_groups", "key": "DA", "status": "active", "payload": {"processes": ["D/A1"]}},
        },
        "agent_v4_table_catalog_items": {
            "table_catalog:wip_today": {"_id": "table_catalog:wip_today", "dataset_key": "wip_today", "status": "active", "payload": {"source_type": "oracle"}},
        },
        "agent_v4_main_flow_filters": {
            "main_flow_filter:DATE": {"_id": "main_flow_filter:DATE", "filter_key": "DATE", "status": "active", "payload": {"aliases": ["오늘"]}},
        },
    }
    export_tool = load_module(ROOT / "tools" / "export_mongodb_metadata_to_json.py")
    upload_tool = load_module(ROOT / "tools" / "upload_json_to_mongodb.py")
    output_path = tmp_path / "metadata_bundle.json"

    export_summary = export_tool.export_metadata_bundle(
        export_tool.MongoExportConfig(
            mongo_uri="mongodb://fake",
            database="datagov",
            collections={
                "domain": "agent_v4_domain_items",
                "table-catalog": "agent_v4_table_catalog_items",
                "main-flow-filter": "agent_v4_main_flow_filters",
            },
        ),
        ["domain", "table-catalog", "main-flow-filter"],
        output_path,
    )
    upload_summary = upload_tool.upload_bundle(
        output_path,
        upload_tool.MongoUploadConfig(
            mongo_uri="mongodb://fake",
            database="portable_datagov",
            collections={
                "domain": "agent_v4_domain_items",
                "table-catalog": "agent_v4_table_catalog_items",
                "main-flow-filter": "agent_v4_main_flow_filters",
            },
        ),
        [],
        mode="upsert",
    )

    assert export_summary["collections"]["domain"]["document_count"] == 1
    assert upload_summary["collections"]["main-flow-filter"]["written_count"] == 1
    assert store["portable_datagov"]["agent_v4_domain_items"]["domain:process_groups:DA"]["payload"]["processes"] == ["D/A1"]
    assert store["portable_datagov"]["agent_v4_table_catalog_items"]["table_catalog:wip_today"]["payload"]["source_type"] == "oracle"
    assert store["portable_datagov"]["agent_v4_main_flow_filters"]["main_flow_filter:DATE"]["payload"]["aliases"] == ["오늘"]


def test_langflow_writer_non_dry_run_requires_explicit_mongo_config(monkeypatch):
    for env_name in (
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "MONGODB_DOMAIN_COLLECTION",
        "MONGODB_TABLE_CATALOG_COLLECTION",
        "MONGODB_MAIN_FLOW_FILTER_COLLECTION",
        "MONGODB_RESULT_COLLECTION",
    ):
        monkeypatch.delenv(env_name, raising=False)
    request_loader = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "00_main_flow_filter_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "04_main_flow_filter_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "07_main_flow_filter_review_writer.py")
    payload = request_loader.build_request("DATE는 기준일 필터야.", "ask", "false")
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "filter_key": "DATE",
                    "payload": {
                        "display_name": "기준일",
                        "aliases": ["날짜", "오늘"],
                        "operator": "eq",
                        "value_type": "date",
                        "value_shape": "scalar",
                    },
                }
            ]
        },
    )

    result = writer.review_and_write(payload, {"ready_to_save": True, "errors": [], "supplement_requests": []})

    assert result["write_result"]["success"] is False
    assert result["write_result"]["errors"][0]["type"] == "missing_mongo_config"


def test_metadata_writers_use_deterministic_review_without_second_llm(monkeypatch):
    install_fake_pymongo(monkeypatch)
    specs = [
        (
            "domain_saving_flow/00_domain_saving_request_loader.py",
            "domain_saving_flow/04_domain_saving_result_normalizer.py",
            "domain_saving_flow/07_domain_review_writer.py",
            {"items": [{"section": "process_groups", "key": "DA", "payload": {"display_name": "D/A"}}]},
            "domain_items",
        ),
        (
            "table_catalog_saving_flow/00_table_catalog_saving_request_loader.py",
            "table_catalog_saving_flow/04_table_catalog_saving_result_normalizer.py",
            "table_catalog_saving_flow/07_table_catalog_review_writer.py",
            {"items": [{"dataset_key": "wip_today", "payload": {"source_type": "oracle", "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT 1"}}}]},
            "table_items",
        ),
        (
            "main_flow_filters_saving_flow/00_main_flow_filter_saving_request_loader.py",
            "main_flow_filters_saving_flow/04_main_flow_filter_saving_result_normalizer.py",
            "main_flow_filters_saving_flow/07_main_flow_filter_review_writer.py",
            {"items": [{"filter_key": "DATE", "payload": {"display_name": "기준일", "operator": "eq", "value_type": "date", "value_shape": "scalar"}}]},
            "filter_items",
        ),
    ]
    for request_path, normalizer_path, writer_path, llm_output, collection_name in specs:
        request_loader = load_module(ROOT / "langflow_components" / request_path)
        normalizer = load_module(ROOT / "langflow_components" / normalizer_path)
        writer = load_module(ROOT / "langflow_components" / writer_path)
        payload = request_loader.build_request("live metadata", "replace", False)
        payload = normalizer.normalize_authoring(payload, llm_output)

        result = writer.review_and_write(payload, "not-json", mongo_uri="mongodb://fake", mongo_database="datagov", collection_name=collection_name)

        assert result["review"]["ready_to_save"] is True
        assert result["write_result"]["success"] is True
        assert result["write_result"]["saved_count"] == 1


def test_table_catalog_duplicate_actions_are_distinct_and_raw_trace_is_redacted(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    matcher = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "05_table_catalog_similarity_checker.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    positive_review = {"ready_to_save": True, "errors": [], "supplement_requests": []}

    def run(action: str):
        store["datagov"] = {
            "agent_v4_table_catalog_items": {
                "table_catalog:wip_today": {
                    "_id": "table_catalog:wip_today",
                    "dataset_key": "wip_today",
                    "status": "active",
                    "payload": {
                        "display_name": "Old WIP",
                        "description": "keep-on-merge",
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT OLD"},
                    },
                }
            }
        }
        payload = request_loader.build_request("password=top-secret", action, False)
        payload = normalizer.normalize_authoring(
            payload,
            {"items": [{"dataset_key": "wip_today", "status": "active", "payload": {"display_name": "New WIP", "source_type": "oracle", "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT NEW"}}}]},
        )
        payload = matcher.check_similarity(payload)
        return writer.review_and_write(payload, positive_review)

    skip_result = run("skip")
    assert skip_result["write_result"]["status"] == "skipped"
    assert store["datagov"]["agent_v4_table_catalog_items"]["table_catalog:wip_today"]["payload"]["display_name"] == "Old WIP"

    merge_result = run("merge")
    merged = store["datagov"]["agent_v4_table_catalog_items"]["table_catalog:wip_today"]
    assert merge_result["write_result"]["operation_by_key"][0]["operation"] == "merged"
    assert merged["payload"]["display_name"] == "New WIP"
    assert merged["payload"]["description"] == "keep-on-merge"
    assert "top-secret" not in merged["registration_trace"]["raw_text"]
    assert "***" in merged["registration_trace"]["raw_text"]

    replace_result = run("replace")
    replaced = store["datagov"]["agent_v4_table_catalog_items"]["table_catalog:wip_today"]
    assert replace_result["write_result"]["operation_by_key"][0]["operation"] == "replaced"
    assert "description" not in replaced["payload"]

    create_result = run("create_new")
    collection = store["datagov"]["agent_v4_table_catalog_items"]
    assert create_result["write_result"]["operation_by_key"][0]["operation"] == "created_new"
    assert "table_catalog:wip_today" in collection
    assert "table_catalog:wip_today_copy" in collection


def test_table_catalog_writer_rejects_secret_fields_before_dry_run():
    request_loader = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "00_table_catalog_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    payload = request_loader.build_request("unsafe dataset", "replace", True)
    payload = normalizer.normalize_authoring(
        payload,
        {"items": [{"dataset_key": "unsafe", "payload": {"source_type": "oracle", "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT 1", "password": "should-not-store"}}}]},
    )

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is False
    error_types = {error["type"] for error in result["write_result"]["errors"]}
    assert "credential_field_forbidden" in error_types
    assert "forbidden_source_config_key" in error_types


def test_domain_replace_resolves_unique_alias_to_existing_canonical_key(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    matcher = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "05_domain_similarity_checker.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    response_builder = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "08_domain_saving_response_builder.py")
    message_adapter = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "09_domain_saving_message_adapter.py")
    existing = {
        "_id": "domain:process_groups:BG",
        "section": "process_groups",
        "key": "BG",
        "status": "active",
        "payload": {"display_name": "BG", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2"]},
    }
    store["datagov"] = {"agent_v4_domain_items": {existing["_id"]: deepcopy(existing)}}
    payload = request_loader.build_request("BG 또는 B/G 공정 그룹은 B/G1부터 B/G5까지 포함해.", "replace", False)
    payload = normalizer.normalize_authoring(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "BG_PROCESS_GROUP",
                    "status": "active",
                    "payload": {
                        "display_name": "BG 공정 그룹",
                        "aliases": ["BG", "B/G", "B/G 공정 그룹"],
                        "processes": ["B/G1", "B/G2", "B/G3", "B/G4", "B/G5"],
                    },
                }
            ]
        },
    )
    payload = matcher.check_similarity(payload, {"existing_items": [existing]})
    dry_run_payload = deepcopy(payload)
    dry_run_payload["request"]["dry_run"] = True
    dry_run_result = writer.review_and_write(dry_run_payload)
    dry_operation = dry_run_result["write_result"]["operation_by_key"][0]
    assert dry_operation["operation"] == "replaced"
    assert dry_operation["target_key"] == "process_groups:BG"

    result = writer.review_and_write(payload, mongo_uri="mongodb://fake", mongo_database="datagov", collection_name="agent_v4_domain_items")
    response = response_builder.build_response(result)
    message = message_adapter.build_message(response)

    collection = store["datagov"]["agent_v4_domain_items"]
    assert set(collection) == {"domain:process_groups:BG"}
    assert collection["domain:process_groups:BG"]["payload"]["processes"] == ["B/G1", "B/G2", "B/G3", "B/G4", "B/G5"]
    operation = result["write_result"]["operation_by_key"][0]
    assert operation["operation"] == "replaced"
    assert operation["requested_key"] == "process_groups:BG_PROCESS_GROUP"
    assert operation["target_key"] == "process_groups:BG"
    assert operation["target_id"] == "domain:process_groups:BG"
    assert operation["match_type"] == "identity_overlap"
    assert response["data"]["rows"][0]["키"] == "process_groups:BG"
    assert response["data"]["rows"][0]["처리"] == "기존 항목 교체"
    assert any("기존 항목 교체 1건" in point for point in response["answer_sections"]["key_points"])
    assert "process_groups:BG" in message
    assert "기존 항목 교체" in message


def test_domain_replace_inserts_when_no_similar_existing_item(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    matcher = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "05_domain_similarity_checker.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    payload = request_loader.build_request("신규 CMP 공정 그룹", "replace", False)
    payload = normalizer.normalize_authoring(
        payload,
        {"items": [{"section": "process_groups", "key": "CMP", "payload": {"display_name": "CMP", "aliases": ["CMP"], "processes": ["CMP1"]}}]},
    )
    payload = matcher.check_similarity(payload, {"existing_items": []})

    result = writer.review_and_write(payload, mongo_uri="mongodb://fake", mongo_database="datagov", collection_name="agent_v4_domain_items")

    assert result["write_result"]["success"] is True
    assert result["write_result"]["operation_by_key"][0]["operation"] == "inserted"
    assert "domain:process_groups:CMP" in store["datagov"]["agent_v4_domain_items"]


def test_domain_replace_blocks_ambiguous_alias_without_writing(monkeypatch):
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    matcher = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "05_domain_similarity_checker.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    existing_items = [
        {"_id": "domain:process_groups:BG", "section": "process_groups", "key": "BG", "payload": {"display_name": "BG", "aliases": ["BG", "B/G"], "processes": ["B/G1"]}},
        {"_id": "domain:process_groups:BG_LEGACY", "section": "process_groups", "key": "BG_LEGACY", "payload": {"display_name": "구 BG", "aliases": ["BG", "B/G"], "processes": ["B/G0"]}},
    ]
    store["datagov"] = {"agent_v4_domain_items": {item["_id"]: deepcopy(item) for item in existing_items}}
    before = deepcopy(store["datagov"]["agent_v4_domain_items"])
    payload = {
        "request": {"raw_text": "BG 공정 교체", "duplicate_action": "replace", "dry_run": False},
        "items": [{"section": "process_groups", "key": "BG_PROCESS_GROUP", "payload": {"display_name": "BG 공정 그룹", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2"]}}],
    }
    payload = matcher.check_similarity(payload, {"existing_items": existing_items})

    result = writer.review_and_write(payload, mongo_uri="mongodb://fake", mongo_database="datagov", collection_name="agent_v4_domain_items")

    assert result["write_result"]["success"] is False
    assert result["write_result"]["saved_count"] == 0
    assert result["write_result"]["errors"][0]["type"] == "ambiguous_replace_target"
    assert store["datagov"]["agent_v4_domain_items"] == before


def test_domain_replace_blocks_when_identity_lookup_failed():
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    payload = {
        "request": {"raw_text": "BG 공정 교체", "duplicate_action": "replace", "dry_run": True},
        "items": [{"section": "process_groups", "key": "BG", "payload": {"display_name": "BG", "aliases": ["BG"], "processes": ["B/G1"]}}],
        "trace": {"duplicate_lookup": {"status": "error", "errors": [{"type": "mongo_duplicate_lookup_error", "message": "timeout"}]}},
    }

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is False
    assert result["write_result"]["saved_count"] == 0
    assert result["write_result"]["errors"][0]["type"] == "identity_lookup_unavailable"


def test_domain_identity_matching_is_same_section_exact_and_not_substring():
    matcher = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "05_domain_similarity_checker.py")
    candidate = {"section": "process_groups", "key": "BG_PROCESS_GROUP", "payload": {"display_name": "BG 공정 그룹", "aliases": ["B-G"]}}
    existing = {"_id": "domain:process_groups:BG", "section": "process_groups", "key": "BG", "payload": {"display_name": "BG", "aliases": ["B/G"]}}
    cross_section = {"_id": "domain:metric_terms:BG", "section": "metric_terms", "key": "BG", "payload": {"display_name": "BG", "aliases": ["B/G"]}}
    substring = {"_id": "domain:process_groups:BGA", "section": "process_groups", "key": "BGA", "payload": {"display_name": "BGA", "aliases": ["BGA"]}}

    matched = matcher.check_similarity({"items": [candidate]}, {"existing_items": [existing, cross_section, substring]})

    assert len(matched["existing_matches"]) == 1
    assert matched["existing_matches"][0]["existing_key"] == "process_groups:BG"
    assert matched["existing_matches"][0]["match_type"] == "identity_overlap"

    duplicate = {"_id": "domain:process_groups:BG_PROCESS_GROUP", "section": "process_groups", "key": "BG_PROCESS_GROUP", "payload": {"display_name": "BG 공정 그룹", "aliases": ["BG", "B/G"]}}
    ambiguous = matcher.check_similarity({"items": [candidate]}, {"existing_items": [existing, duplicate]})
    assert ambiguous["existing_matches"][0]["identity_resolution"] == "ambiguous"
    assert set(ambiguous["existing_matches"][0]["existing_candidate_keys"]) == {"process_groups:BG", "process_groups:BG_PROCESS_GROUP"}


def test_metadata_saving_response_message_and_api_nodes_are_separated():
    specs = [
        {
            "metadata_type": "domain",
            "response_path": ROOT
            / "langflow_components"
            / "domain_saving_flow"
            / "08_domain_saving_response_builder.py",
            "message_path": ROOT
            / "langflow_components"
            / "domain_saving_flow"
            / "09_domain_saving_message_adapter.py",
            "api_path": ROOT
            / "langflow_components"
            / "domain_saving_flow"
            / "10_domain_saving_api_response_builder.py",
            "items": [
                {
                    "section": "process_groups",
                    "key": "DA",
                    "payload": {"display_name": "D/A"},
                }
            ],
        },
        {
            "metadata_type": "table_catalog",
            "response_path": ROOT
            / "langflow_components"
            / "table_catalog_saving_flow"
            / "08_table_catalog_saving_response_builder.py",
            "message_path": ROOT
            / "langflow_components"
            / "table_catalog_saving_flow"
            / "09_table_catalog_saving_message_adapter.py",
            "api_path": ROOT
            / "langflow_components"
            / "table_catalog_saving_flow"
            / "10_table_catalog_saving_api_response_builder.py",
            "items": [
                {
                    "dataset_key": "production_today",
                    "payload": {
                        "display_name": "Production Today",
                        "dataset_family": "production",
                        "source_type": "oracle",
                        "required_params": ["DATE"],
                    },
                }
            ],
        },
        {
            "metadata_type": "main_flow_filter",
            "response_path": ROOT
            / "langflow_components"
            / "main_flow_filters_saving_flow"
            / "08_main_flow_filter_saving_response_builder.py",
            "message_path": ROOT
            / "langflow_components"
            / "main_flow_filters_saving_flow"
            / "09_main_flow_filter_saving_message_adapter.py",
            "api_path": ROOT
            / "langflow_components"
            / "main_flow_filters_saving_flow"
            / "10_main_flow_filter_saving_api_response_builder.py",
            "items": [
                {
                    "filter_key": "DATE",
                    "payload": {
                        "display_name": "기준일",
                        "operator": "eq",
                        "value_type": "date",
                        "value_shape": "scalar",
                    },
                }
            ],
        },
    ]

    for spec in specs:
        response_module = load_module(spec["response_path"])
        message_module = load_module(spec["message_path"])
        api_module = load_module(spec["api_path"])

        payload = response_module.build_response(
            {
                "metadata_type": spec["metadata_type"],
                "items": spec["items"],
                "write_result": {
                    "success": True,
                    "ready_to_save": True,
                    "dry_run": True,
                    "saved_count": 0,
                    "would_save_count": len(spec["items"]),
                    "message": "드라이런입니다. MongoDB에는 저장하지 않았습니다.",
                },
                "review": {"ready_to_save": True, "errors": [], "supplement_requests": []},
                "trace": {"raw_text_preview": "테스트 원문"},
            }
        )
        message = message_module.build_message(payload)
        api_response = api_module.build_api_response(payload, message)

        assert payload["response_type"] == "metadata_authoring"
        assert payload["metadata_type"] == spec["metadata_type"]
        assert payload["answer_sections"]["target_table"]["row_count"] == len(spec["items"])
        assert "### 등록 결과" in message
        assert "### 한눈에 보기" in message
        assert "### 등록 대상" in message
        assert "### 다음 단계" in message
        assert api_response["response_type"] == "metadata_authoring"
        assert api_response["metadata_type"] == spec["metadata_type"]
        assert api_response["message"] == message
        assert "display_message" not in api_response
        assert "answer_message" not in api_response
        assert api_response["answer_sections"]["target_table"]["row_count"] == len(spec["items"])
        assert api_response["answer_sections"]["target_table"]["row_source"] == "data.rows"
        assert "rows" not in api_response["answer_sections"]["target_table"]

        response_outputs = [item.kwargs.get("name") for item in _component_outputs(response_module)]
        message_outputs = [item.kwargs.get("name") for item in _component_outputs(message_module)]
        api_outputs = [item.kwargs.get("name") for item in _component_outputs(api_module)]
        assert response_outputs == ["payload_out"]
        assert message_outputs == ["message"]
        assert api_outputs == ["api_response", "api_message"]


def test_metadata_qa_empty_question_stops_loaders_and_returns_error():
    request_loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "00_metadata_qa_request_loader.py")
    snapshot_loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "01_mongodb_metadata_snapshot_loader.py")
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    guarded_agent = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_guarded_agent.py")
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    payload = request_loader.build_request("")

    snapshot = snapshot_loader.load_metadata_snapshot(payload, mongo_uri="mongodb://must-not-connect")
    assert snapshot["metadata_snapshot"]["status"] == "skipped"
    assert snapshot["metadata_snapshot"]["errors"][0]["type"] == "empty_question"
    assert snapshot["domain_items"] == snapshot["table_catalog_items"] == snapshot["main_flow_filters"] == []

    context_payload = context_builder.build_metadata_qa_context(
        payload,
        {"domain_items": [{"section": "process_groups", "key": "DA", "payload": {}}]},
        {"table_catalog_items": [{"dataset_key": "production_today", "payload": {}}]},
        {"main_flow_filters": [{"filter_key": "DATE", "payload": {}}]},
    )
    answer = normalizer.normalize_metadata_qa_response(context_payload, '{"answer_message":"사용하면 안 되는 LLM 답변"}')
    assert guarded_agent.should_skip_agent(context_payload) is True

    assert context_payload["metadata_qa_context"]["llm_control"] == {"skip": True, "reason": "empty_question"}
    assert context_payload["metadata_qa_context"]["source_refs"] == []
    assert answer["status"] == "error"
    assert answer["answer_type"] == "invalid_request"
    assert "질문이 비어" in answer["answer_message"]
    assert answer["trace"]["inspection"]["metadata_qa_response"]["used_llm_response"] is False


def test_metadata_qa_snapshot_loader_uses_one_client_and_short_process_cache(monkeypatch):
    setattr(builtins, "_metadata_driven_v5_qa_snapshot_cache_v1", {"generation": 0, "entries": {}})
    store = install_fake_pymongo(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {
            "domain:process_groups:DA": {"_id": "domain:process_groups:DA", "section": "process_groups", "key": "DA", "status": "active", "payload": {"processes": ["D/A1"]}},
        },
        "agent_v4_table_catalog_items": {
            "table_catalog:production_today": {"_id": "table_catalog:production_today", "dataset_key": "production_today", "status": "active", "payload": {"source_type": "oracle"}},
        },
        "agent_v4_main_flow_filters": {
            "main_flow_filter:DATE": {"_id": "main_flow_filter:DATE", "filter_key": "DATE", "status": "active", "payload": {"operator": "eq"}},
        },
    }
    loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "01_mongodb_metadata_snapshot_loader.py")
    request = {"request": {"question": "등록된 메타데이터를 알려줘"}}

    first = loader.load_metadata_snapshot(request, "mongodb://fake", "datagov", cache_ttl_seconds="30")
    first_client_count = sys.modules["pymongo"].metrics["client_count"]
    store["datagov"]["agent_v4_domain_items"]["domain:process_groups:BG"] = {
        "_id": "domain:process_groups:BG",
        "section": "process_groups",
        "key": "BG",
        "status": "active",
        "payload": {"processes": ["B/G1"]},
    }
    cached = loader.load_metadata_snapshot(request, "mongodb://fake", "datagov", cache_ttl_seconds="30")
    refreshed = loader.load_metadata_snapshot(request, "mongodb://fake", "datagov", cache_ttl_seconds="0")

    assert first["metadata_snapshot"]["count"] == 3
    assert first["metadata_snapshot"]["cache_hit"] is False
    assert first_client_count == 1
    assert sys.modules["pymongo"].metrics["client_count"] == 2
    assert cached["metadata_snapshot"]["count"] == 3
    assert cached["metadata_snapshot"]["cache_hit"] is True
    assert refreshed["metadata_snapshot"]["count"] == 4
    assert refreshed["metadata_snapshot"]["cache_hit"] is False


def test_successful_metadata_write_invalidates_same_process_qa_snapshot(monkeypatch):
    setattr(builtins, "_metadata_driven_v5_qa_snapshot_cache_v1", {"generation": 0, "entries": {}})
    store = install_fake_pymongo(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {},
        "agent_v4_table_catalog_items": {},
        "agent_v4_main_flow_filters": {},
    }
    snapshot_loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "01_mongodb_metadata_snapshot_loader.py")
    request_loader = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "00_domain_saving_request_loader.py")
    normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    qa_request = {"request": {"question": "등록된 공정 그룹을 알려줘"}}

    first = snapshot_loader.load_metadata_snapshot(qa_request, "mongodb://fake", "datagov", cache_ttl_seconds="30")
    authoring = request_loader.build_request("CMP는 CMP1 공정입니다.", "replace", False)
    authoring = normalizer.normalize_authoring(
        authoring,
        {"items": [{"section": "process_groups", "key": "CMP", "payload": {"display_name": "CMP", "aliases": ["CMP"], "processes": ["CMP1"]}}]},
    )
    write_result = writer.review_and_write(authoring, mongo_uri="mongodb://fake", mongo_database="datagov", collection_name="agent_v4_domain_items")
    refreshed = snapshot_loader.load_metadata_snapshot(qa_request, "mongodb://fake", "datagov", cache_ttl_seconds="30")

    assert first["metadata_snapshot"]["count"] == 0
    assert write_result["write_result"]["success"] is True
    assert write_result["write_result"]["metadata_qa_snapshot_invalidated"] is True
    assert refreshed["metadata_snapshot"]["cache_hit"] is False
    assert refreshed["metadata_snapshot"]["generation"] == 1
    assert refreshed["metadata_snapshot"]["count"] == 1


def test_metadata_qa_deterministic_mode_stops_llm_branch_and_uses_direct_answer():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    guarded_agent = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_guarded_agent.py")
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    payload = {"request": {"question": "DA공정에는 어떤 세부 공정이 있어?"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    domain_items = {"domain_items": [{"section": "process_groups", "key": "DA", "payload": {"display_name": "D/A", "aliases": ["DA"], "processes": ["D/A1", "D/A2"]}}]}

    context_payload = context_builder.build_metadata_qa_context(payload, domain_items, {}, {})
    agent = guarded_agent.MetadataQaGuardedAgent()
    agent.control_payload = context_payload
    agent._stub_response = '{"answer_message":"사용되면 안 되는 LLM 답변"}'
    agent_response = asyncio.run(agent.message_response())
    answer = normalizer.normalize_metadata_qa_response(context_payload, agent_response)

    assert context_payload["metadata_qa_context"]["llm_control"]["eligible_to_skip"] is True
    assert context_payload["metadata_qa_context"]["llm_control"]["skip"] is True
    assert agent_response.text == ""
    assert agent.status.startswith("LLM skipped")
    assert answer["answer_message"] != "사용되면 안 되는 LLM 답변"
    assert answer["trace"]["inspection"]["metadata_qa_response"]["llm_skipped"] is True


def test_metadata_qa_free_form_mode_keeps_llm_branch_and_embedded_response():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    guarded_agent = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_guarded_agent.py")
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    payload = {"request": {"question": "생산량이라는 용어를 쉽게 설명해줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}
    domain_items = {"domain_items": [{"section": "quantity_terms", "key": "production_quantity", "payload": {"display_name": "생산량", "aliases": ["생산실적"], "column": "PRODUCTION"}}]}

    context_payload = context_builder.build_metadata_qa_context(payload, domain_items, {}, {})
    agent = guarded_agent.MetadataQaGuardedAgent()
    agent.control_payload = context_payload
    agent._stub_response = '{"answer_type":"term_definition","answer_message":"생산량은 일정 기간 실제로 생산된 수량입니다."}'
    agent_response = asyncio.run(agent.message_response())
    answer = normalizer.normalize_metadata_qa_response(context_payload, agent_response)

    assert context_payload["metadata_qa_context"]["llm_control"]["skip"] is False
    assert agent.status == "LLM enabled: free-form metadata QA"
    assert answer["answer_message"] == "생산량은 일정 기간 실제로 생산된 수량입니다."
    assert answer["trace"]["inspection"]["metadata_qa_response"]["used_llm_response"] is True


def test_metadata_qa_response_status_reflects_upstream_load_errors():
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    payload = {
        "request": {"question": "DA 공정 그룹을 알려줘"},
        "metadata_qa_context": {
            "question": "DA 공정 그룹을 알려줘",
            "answer_mode": "process_group",
            "candidate_rows": [{"metadata_type": "domain", "key": "DA", "display_name": "D/A"}],
            "source_refs": [{"metadata_type": "domain", "section": "process_groups", "key": "DA"}],
            "llm_control": {"skip": False},
        },
        "trace": {"warnings": [], "errors": [{"type": "mongo_load_error", "message": "table loader failed"}], "inspection": {}},
    }

    partial = normalizer.normalize_metadata_qa_response(payload, '{"answer_message":"D/A 공정 그룹입니다."}')
    failed_payload = deepcopy(payload)
    failed_payload["metadata_qa_context"]["candidate_rows"] = []
    failed_payload["metadata_qa_context"]["source_refs"] = []
    failed = normalizer.normalize_metadata_qa_response(failed_payload, '{"answer_message":"조회할 수 없습니다."}')

    assert partial["status"] == "partial"
    assert partial["trace"]["inspection"]["metadata_qa_response"]["status"] == "partial"
    assert failed["status"] == "error"


def test_metadata_qa_flow_reads_shared_v4_metadata_and_emits_api_contract(monkeypatch):
    setattr(builtins, "_metadata_driven_v5_qa_snapshot_cache_v1", {"generation": 0, "entries": {}})
    store = install_fake_pymongo(monkeypatch)
    set_shared_v4_mongo_env(monkeypatch)
    store["datagov"] = {
        "agent_v4_domain_items": {
            "domain:quantity_terms:production_quantity": {
                "_id": "domain:quantity_terms:production_quantity",
                "section": "quantity_terms",
                "key": "production_quantity",
                "status": "active",
                "raw_trace": {"hidden": True},
                "payload": {
                    "display_name": "생산량",
                    "aliases": ["생산량", "생산실적"],
                    "column": "PRODUCTION",
                    "aggregation_method": "sum",
                },
            }
        },
        "agent_v4_table_catalog_items": {
            "table_catalog:production_today": {
                "_id": "table_catalog:production_today",
                "dataset_key": "production_today",
                "status": "active",
                "payload": {
                    "display_name": "Production Today",
                    "dataset_family": "production",
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "PNT_RPT",
                        "query_template": "SELECT WORK_DATE, DEVICE, PRODUCTION FROM PROD WHERE WORK_DATE = {DATE}",
                    },
                },
            }
        },
        "agent_v4_main_flow_filters": {
            "main_flow_filter:DATE": {
                "_id": "main_flow_filter:DATE",
                "filter_key": "DATE",
                "status": "active",
                "payload": {"display_name": "기준일", "aliases": ["오늘", "어제"], "operator": "eq"},
            }
        },
    }
    request_loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "00_metadata_qa_request_loader.py")
    snapshot_loader = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "01_mongodb_metadata_snapshot_loader.py")
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    variables_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_variables_builder.py")
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    message_adapter = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "05_metadata_qa_message_adapter.py")
    api_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "06_metadata_qa_api_response_builder.py")

    payload = request_loader.build_request("생산량 데이터 관련 쿼리문은 어떤건지 알려줘")
    snapshot = snapshot_loader.load_metadata_snapshot(payload)
    domain = snapshot_loader._output_payload(snapshot, "domain_items")
    table = snapshot_loader._output_payload(snapshot, "table_catalog_items")
    main_filter = snapshot_loader._output_payload(snapshot, "main_flow_filters")
    context_payload = context_builder.build_metadata_qa_context(payload, domain, table, main_filter)
    variables = variables_builder.build_variables(context_payload)
    qa_payload = normalizer.normalize_metadata_qa_response(context_payload, "")
    message = message_adapter.build_message(qa_payload)
    api_response = api_builder.build_api_response(qa_payload, message)

    assert context_payload["metadata_route"]["answer_mode"] == "dataset_sql"
    assert "raw_trace" not in variables["metadata_context_json"]
    assert "production_today" in variables["metadata_context_json"]
    assert qa_payload["response_type"] == "metadata_qa"
    assert qa_payload["direct_response_ready"] is True
    assert qa_payload["answer_sections"]["sql_blocks"][0]["sql"].startswith("SELECT WORK_DATE")
    assert "```sql" in message
    assert api_response["response_type"] == "metadata_qa"
    assert "metadata_qa_context" not in api_response
    assert "agent_v4_result_store" not in store["datagov"]
    assert sys.modules["pymongo"].metrics["client_count"] == 1


def test_metadata_qa_sections_support_process_group_and_data_redirect():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    message_adapter = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "05_metadata_qa_message_adapter.py")
    api_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "06_metadata_qa_api_response_builder.py")
    domain_items = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "DA",
                "payload": {
                    "display_name": "D/A",
                    "aliases": ["DA", "D/A"],
                    "processes": ["D/A1", "D/A2", "D/A3"],
                },
            }
        ]
    }
    table_items = {
        "table_catalog_items": [
            {
                "dataset_key": "production_today",
                "payload": {
                    "display_name": "Production Today",
                    "dataset_family": "production",
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                },
            }
        ]
    }

    process_payload = context_builder.build_metadata_qa_context(
        {"request": {"question": "DA공정에는 어떤 세부 공정이 있어?"}, "trace": {"warnings": [], "errors": [], "inspection": {}}},
        domain_items,
        table_items,
        {},
    )
    process_answer = normalizer.normalize_metadata_qa_response(process_payload, "")
    process_message = message_adapter.build_message(process_answer)
    process_api = api_builder.build_api_response(process_answer, process_message)

    assert process_payload["metadata_route"]["answer_mode"] == "process_group"
    assert process_answer["answer_type"] == "process_group"
    assert process_answer["answer_sections"]["detail_table"]["title"] == "공정 그룹"
    assert "### 공정 그룹" in process_message
    assert process_api["answer_type"] == "process_group"
    assert process_api["answer_sections"]["detail_table"]["row_count"] == 1

    redirect_payload = context_builder.build_metadata_qa_context(
        {"request": {"question": "오늘 DA공정 생산량 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}},
        domain_items,
        table_items,
        {},
    )
    redirect_answer = normalizer.normalize_metadata_qa_response(redirect_payload, "")
    redirect_message = message_adapter.build_message(redirect_answer)

    assert redirect_payload["metadata_route"]["answer_mode"] == "data_analysis_redirect"
    assert redirect_answer["answer_type"] == "data_analysis_redirect"
    assert redirect_answer["answer_sections"]["route_hint"]["target_route"] == "data_analysis"
    assert "### 권장 실행 경로" in redirect_message


def test_metadata_qa_available_sources_keeps_complete_context_table():
    normalizer = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "04_metadata_qa_response_normalizer.py")
    message_adapter = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "05_metadata_qa_message_adapter.py")
    rows = [
        {
            "metadata_type": "table_catalog",
            "key": f"dataset_{index}",
            "display_name": f"Dataset {index}",
            "source_type": "oracle",
            "required_params": "DATE",
        }
        for index in range(1, 8)
    ]
    payload = {
        "request": {"question": "지금 조회 가능한 데이터셋 목록과 필수 조건을 표로 보여줘"},
        "metadata_qa_context": {
            "answer_mode": "available_sources",
            "candidate_rows": rows,
            "source_refs": [{"metadata_type": "table_catalog", "key": row["key"]} for row in rows],
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    llm_response = json.dumps(
        {
            "answer_type": "available_sources",
            "answer_message": "현재 조회 가능한 데이터셋 목록입니다.",
            "source_refs": [{"metadata_type": "table_catalog", "key": row["key"]} for row in rows[:5]],
            "answer_sections": {
                "summary": {"headline": "현재 조회 가능한 데이터셋 목록입니다."},
                "detail_table": {
                    "title": "조회 가능한 데이터셋 목록",
                    "columns": ["key", "display_name", "source_type", "required_params"],
                    "rows": rows[:5],
                    "row_count": 5,
                },
                "related_items": [{"metadata_type": "table_catalog", "key": row["key"]} for row in rows[:5]],
            },
        },
        ensure_ascii=False,
    )

    answer = normalizer.normalize_metadata_qa_response(payload, llm_response)

    assert "7" in answer["answer_message"]
    assert answer["answer_sections"]["detail_table"]["row_count"] == 7
    assert answer["answer_sections"]["detail_table"]["row_source"] == "data.rows"
    assert "rows" not in answer["answer_sections"]["detail_table"]
    assert len(answer["data"]["rows"]) == 7
    assert answer["answer_sections"]["detail_table"]["columns"] == ["데이터셋", "데이터셋 키", "분류", "연결 방식", "DB/소스", "필수 조건"]
    assert "metadata_type" not in answer["answer_sections"]["detail_table"]["columns"]
    assert answer["answer_sections"]["key_points"]
    assert answer["answer_sections"]["related_items"] == []
    assert answer["answer_sections"]["show_related_items"] is False
    assert len(answer["metadata_qa"]["source_refs"]) == 7
    assert answer["data"]["row_count"] == 7
    assert answer["trace"]["inspection"]["metadata_qa_response"]["used_context_table"] is True
    message = message_adapter.build_message(answer)
    assert "### 한눈에 보기" in message
    assert "### 다음에 물어볼 수 있는 질문" in message
    assert "### 사용한 메타데이터" not in message
    assert "metadata_type" not in message


def test_metadata_qa_available_sources_question_honors_small_limit():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    table_items = {
        "table_catalog_items": [
            {
                "dataset_key": f"dataset_{index}",
                "payload": {
                    "display_name": f"Dataset {index}",
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                },
            }
            for index in range(1, 10)
        ]
    }
    payload = {
        "request": {"question": "지금 조회 가능한 데이터셋 목록과 각 데이터셋의 연결 방식, 필수 조건을 표로 보여줘"},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    context_payload = context_builder.build_metadata_qa_context(payload, {}, table_items, {}, max_items="5")

    assert context_payload["metadata_route"]["answer_mode"] == "available_sources"
    assert context_payload["trace"]["inspection"]["metadata_qa_context"]["dataset_match_count"] == 5
    assert len(context_payload["metadata_qa_context"]["candidate_rows"]) == 5
    assert [row["key"] for row in context_payload["metadata_qa_context"]["candidate_rows"]] == [f"dataset_{index}" for index in range(1, 6)]


def test_metadata_qa_variables_keep_static_policy_inside_prompt_template():
    variables_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_variables_builder.py")
    prompt_text = (ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_prompt_template_ko.md").read_text(encoding="utf-8")

    output_names = [item.kwargs.get("name") for item in variables_builder.MetadataQaVariablesBuilder.outputs]
    variables = variables_builder.build_variables({"request": {"question": "생산량 도메인 알려줘"}, "metadata_qa_context": {}})

    assert output_names == ["question", "metadata_context_json", "output_schema_json"]
    assert "response_policy" not in variables
    assert "{response_policy}" not in prompt_text
    assert "응답 정책:" in prompt_text


def test_metadata_qa_available_sources_context_excludes_sql_and_honors_byte_budget():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    variables_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "03_metadata_qa_variables_builder.py")
    table_items = {
        "table_catalog_items": [
            {
                "dataset_key": f"dataset_{index}",
                "status": "active",
                "payload": {
                    "display_name": f"Dataset {index}",
                    "description": "D" * 5000,
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                    "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT " + "Q" * 5000},
                },
            }
            for index in range(30)
        ]
    }
    payload = {"request": {"question": "조회 가능한 데이터셋 목록과 연결 방식을 보여줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    result = context_builder.build_metadata_qa_context(payload, {}, table_items, {}, max_items="20", max_bytes="12000")
    variables = variables_builder.build_variables(result)

    assert result["metadata_route"]["answer_mode"] == "available_sources"
    assert len(variables["metadata_context_json"].encode("utf-8")) <= 12000 + 2000
    assert "query_template" not in variables["metadata_context_json"]
    assert result["trace"]["inspection"]["metadata_qa_context"]["context_bytes"] <= 12000


def test_metadata_qa_dataset_sql_context_includes_only_selected_sql():
    context_builder = load_module(ROOT / "langflow_components" / "metadata_qa_flow" / "02_metadata_qa_context_builder.py")
    table_items = {
        "table_catalog_items": [
            {"dataset_key": "production_today", "payload": {"display_name": "Production Today", "source_type": "oracle", "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT PROD_SQL"}}},
            {"dataset_key": "wip_today", "payload": {"display_name": "WIP Today", "source_type": "oracle", "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT WIP_SQL"}}},
        ]
    }
    payload = {"request": {"question": "production_today SQL 쿼리 알려줘"}, "trace": {"warnings": [], "errors": [], "inspection": {}}}

    result = context_builder.build_metadata_qa_context(payload, {}, table_items, {}, max_items="1")
    context_text = json.dumps(result["metadata_qa_context"], ensure_ascii=False)

    assert result["metadata_route"]["answer_mode"] == "dataset_sql"
    assert "SELECT PROD_SQL" in context_text
    assert "SELECT WIP_SQL" not in context_text


def test_route_flow_source_layout_matches_current_06_and_07_routers():
    route_dir = ROOT / "langflow_components" / "route_flow"
    route_v2_dir = ROOT / "langflow_components" / "route_flow_v2"

    assert sorted(path.name for path in route_dir.glob("*.py")) == ["01_flow_api_message_caller.py"]
    assert sorted(path.name for path in route_v2_dir.glob("*.py")) == ["01_cached_named_run_flow_tool.py"]
    for obsolete in ("router_flow", "router_flow_v2", "router_flow_v3", "router_tool_flow"):
        assert not (ROOT / "langflow_components" / obsolete).exists()


def test_all_current_flow_artifacts_have_real_custom_component_sources():
    validator = load_module(ROOT / "tools" / "validate_flow_component_sources.py")
    result = validator.audit_repository()

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert result["active_unique_source_files"] == 68
    assert result["all_component_python_files"] == 69
    assert result["support_source_files"] == [
        "langflow_components/data_analysis_flow/function_case_helper_code_input_example.py"
    ]
    assert result["inactive_source_files"] == []
    assert {
        (report["label"], report["flow_count"], report["custom_node_instances"], report["unique_source_files"])
        for report in result["reports"]
    } == {
        ("flow_exports", 7, 77, 68),
        ("import_ready_individual", 7, 77, 68),
        ("import_ready_bundle", 7, 77, 68),
    }


def test_route_flow_06_docs_cover_current_api_router_contract():
    route_dir = ROOT / "langflow_components" / "route_flow"
    guide = (route_dir / "CONNECTION_GUIDE.md").read_text(encoding="utf-8")
    examples = (route_dir / "EXAMPLE_QUESTIONS.md").read_text(encoding="utf-8")
    design = (route_dir / "ROUTE_FLOW_API_DESIGN.md").read_text(encoding="utf-8")

    assert "Smart Router" in guide
    assert "Run API" in guide
    assert "direct_answer" in guide
    assert "clarification" in guide
    assert "Route Message" in guide
    assert "dummy_" not in guide
    assert "오늘 DA공정 생산량 알려줘" in examples
    assert "production_today 필수 조건 보여줘" in examples
    assert "metadata 종류" in examples
    assert "Message" in design and "Data" in design


def test_route_flow_v2_docs_cover_exactly_five_current_tools():
    route_dir = ROOT / "langflow_components" / "route_flow_v2"
    guide = (route_dir / "CONNECTION_GUIDE.md").read_text(encoding="utf-8")
    system_prompt = (route_dir / "SYSTEM_PROMPT_KO.md").read_text(encoding="utf-8")
    tool_descriptions = (route_dir / "TOOL_DESCRIPTIONS.md").read_text(encoding="utf-8")
    examples = (route_dir / "EXAMPLE_QUESTIONS.md").read_text(encoding="utf-8")

    assert "Agent" in guide and "Tool" in guide
    assert "정확히 하나" in system_prompt
    assert "요약하거나 재작성하지 않습니다" in system_prompt
    for slug in (
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
    ):
        assert slug in guide
        assert slug in system_prompt
        assert slug in tool_descriptions
        assert slug in examples
    assert "dummy_" not in guide
    assert "dummy_" not in system_prompt
    assert "dummy_" not in tool_descriptions
    assert "dummy_" not in examples


def test_route_flow_calls_langflow_api_with_branch_message_as_input():
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "outputs": [
                    {
                        "outputs": [
                            {
                                "results": {
                                    "message": {
                                        "text": "오늘 DA공정 생산량은 1,234입니다.",
                                    }
                                }
                            }
                        ]
                    }
                ]
            }

    def fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: int) -> FakeResponse:
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse()

    result = caller.run_flow_api_message(
        "오늘 DA공정 생산량 알려줘",
        api_url="http://localhost:7860/api/v1/run/data-flow",
        api_key="secret",
        session_id="router-session-1",
        timeout_seconds="33",
        post_func=fake_post,
    )

    assert result["status"] == "ok"
    assert result["message"] == "오늘 DA공정 생산량은 1,234입니다."
    assert result["request_body"] == {
        "input_value": "오늘 DA공정 생산량 알려줘",
        "input_type": "chat",
        "output_type": "chat",
        "tweaks": {
            "Chat Input": {"should_store_message": False},
            "Chat Output": {"should_store_message": False},
        },
        "session_id": "router-session-1",
    }
    assert calls == [
        {
            "url": "http://localhost:7860/api/v1/run/data-flow",
            "json": {
                "input_value": "오늘 DA공정 생산량 알려줘",
                "input_type": "chat",
                "output_type": "chat",
                "tweaks": {
                    "Chat Input": {"should_store_message": False},
                    "Chat Output": {"should_store_message": False},
                },
                "session_id": "router-session-1",
            },
            "headers": {"Content-Type": "application/json", "x-api-key": "secret"},
            "timeout": 33,
        }
    ]


def test_route_flow_blocks_route_json_message_before_api_call():
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("API should not be called when Smart Router Route Message is used as input")

    result = caller.run_flow_api_message(
        '{"route":"data_analysis"}',
        api_url="http://localhost:7860/api/v1/run/data-flow",
        post_func=fake_post,
    )

    assert result["status"] == "error"
    assert result["errors"][0]["type"] == "route_message_used_as_input"
    assert "Route Message" in result["message"]
    assert calls == []


def test_route_flow_preserves_saving_raw_text_as_api_input_value():
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    raw_text = "  -- production today 등록\nWITH base AS (\n  SELECT * FROM PROD_TABLE\n)\nSELECT * FROM base\n"
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"outputs": [{"outputs": [{"results": {"message": {"text": "저장되었습니다."}}}]}]}

    def fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: int) -> FakeResponse:
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse()

    result = caller.run_flow_api_message(
        raw_text,
        api_url="http://localhost:7860/api/v1/run/table-flow",
        post_func=fake_post,
    )

    assert result["status"] == "ok"
    assert result["request_body"]["input_value"] == raw_text
    assert calls[0]["json"]["input_value"] == raw_text


def test_route_flow_api_helper_accepts_session_source_for_backward_compatibility():
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    calls = []

    class SessionSource:
        session_id = "shared-router-session"

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"api_response": {"status": "ok", "message": "완료"}}

    def fake_post(url, json, headers, timeout):
        calls.append({"json": json, "timeout": timeout})
        return FakeResponse()

    result = caller.run_flow_api_message(
        "Smart Router가 전달한 원문",
        api_url="http://localhost:7860/api/v1/run/flow",
        session_source_value=SessionSource(),
        connect_timeout_seconds="3",
        read_timeout_seconds="45",
        route_name="metadata_qa",
        post_func=fake_post,
    )

    assert result["status"] == "ok"
    assert result["route_name"] == "metadata_qa"
    assert calls[0]["json"]["session_id"] == "shared-router-session"
    assert calls[0]["timeout"] == (3, 45)


def test_route_flow_uses_240_second_default_child_read_timeout():
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"api_response": {"status": "ok", "message": "완료"}}

    def fake_post(url, json, headers, timeout):
        calls.append(timeout)
        return FakeResponse()

    result = caller.run_flow_api_message(
        "질문",
        api_url="http://localhost:7860/api/v1/run/flow",
        post_func=fake_post,
    )

    assert result["status"] == "ok"
    assert calls == [(5, 240)]


def test_route_flow_resolves_endpoint_name_and_environment_credentials(monkeypatch):
    caller = load_module(ROOT / "langflow_components" / "route_flow" / "01_flow_api_message_caller.py")
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"api_response": {"status": "ok", "message": "완료"}}

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "headers": headers})
        return FakeResponse()

    monkeypatch.setenv("LANGFLOW_BASE_URL", "http://langflow.internal:7860/")
    monkeypatch.setenv("LANGFLOW_API_KEY", "environment-secret")
    result = caller.run_flow_api_message(
        "질문",
        api_url="metadata-driven-v5-data-analysis",
        post_func=fake_post,
    )

    assert result["status"] == "ok"
    assert calls == [
        {
            "url": "http://langflow.internal:7860/api/v1/run/metadata-driven-v5-data-analysis",
            "headers": {"Content-Type": "application/json", "x-api-key": "environment-secret"},
        }
    ]


def test_route_flow_docs_and_component_expose_message_and_status_contract():
    route_dir = ROOT / "langflow_components" / "route_flow"
    py_files = sorted(path.name for path in route_dir.glob("*.py"))
    component = load_module(route_dir / "01_flow_api_message_caller.py")
    source = (route_dir / "01_flow_api_message_caller.py").read_text(encoding="utf-8")
    guide = (route_dir / "CONNECTION_GUIDE.md").read_text(encoding="utf-8")
    design = (route_dir / "ROUTE_FLOW_API_DESIGN.md").read_text(encoding="utf-8")

    assert py_files == ["01_flow_api_message_caller.py"]
    assert [item.kwargs.get("name") for item in _component_inputs(component)] == [
        "flow_input",
        "api_url",
        "api_key",
        "session_id",
        "route_name",
        "connect_timeout_seconds",
        "read_timeout_seconds",
    ]
    assert [item.kwargs.get("name") for item in _component_outputs(component)] == ["message", "status_data"]
    component_inputs = {item.kwargs.get("name"): item.kwargs for item in _component_inputs(component)}
    assert component_inputs["connect_timeout_seconds"].get("value") == "5"
    assert component_inputs["read_timeout_seconds"].get("value") == "240"
    assert "ROUTE_TO_FLOW" not in source
    assert "ROUTE_ALIASES" not in source
    assert "selected_flow" not in source
    assert "API 호출 route의 Smart Router `Route Message`는 비웁니다." in guide
    assert "session_id" in guide
    assert "01 -> 02 -> 03" not in guide
    assert "구조화 상태" in guide
    assert "중간 Gate" in guide
    assert "Message" in design and "Data" in design


def test_cached_named_run_flow_tool_has_compact_schema_cache_and_session_contract(monkeypatch):
    path = ROOT / "langflow_components" / "route_flow_v2" / "01_cached_named_run_flow_tool.py"
    component = load_module(path)
    inputs = {item.kwargs.get("name"): item.kwargs for item in _component_inputs(component)}
    outputs = {item.kwargs.get("name"): item.kwargs for item in _component_outputs(component)}

    assert list(inputs) == [
        "flow_name_selected",
        "flow_id_selected",
        "session_id",
        "cache_flow",
        "tool_name",
        "tool_description",
        "return_direct",
    ]
    assert inputs["flow_id_selected"]["value"] == ""
    assert inputs["session_id"]["advanced"] is True
    assert inputs["cache_flow"]["value"] is True
    assert inputs["return_direct"]["value"] is True
    assert list(outputs) == ["component_as_tool"]
    assert outputs["component_as_tool"]["types"] == ["Tool"]
    source = path.read_text(encoding="utf-8")
    assert '.get("type") == "ChatInput"' in source
    assert '.get("type") == "ChatOutput"' in source
    assert '"should_store_message": False' in source
    assert '"name": "question"' in source
    assert 'name="lazy_flow_result"' in source
    assert "def _run_selected_flow" in source
    assert "get_new_fields_from_graph" not in source
    assert "def _build_flow_tweak_data" in source
    assert "flow_id_selected=None" in source
    assert "tool.return_direct" in source
    assert "parent_session" in source
    assert "session_source" not in source

    vertices = [
        types.SimpleNamespace(id="ChatInput-runtime", data={"type": "ChatInput"}, display_name="Chat Input"),
        types.SimpleNamespace(id="ChatOutput-runtime", data={"type": "ChatOutput"}, display_name="Chat Output"),
    ]
    assert component._single_chat_input_id(vertices) == "ChatInput-runtime"
    assert component._single_chat_output_id(vertices) == "ChatOutput-runtime"
    assert component._question_tweaks(
        "ChatInput-runtime",
        {"question": "현재 등록된 데이터셋 알려줘"},
        "ChatOutput-runtime",
    ) == {
        "ChatInput-runtime": {
            "input_value": "현재 등록된 데이터셋 알려줘",
            "should_store_message": False,
        },
        "ChatOutput-runtime": {"should_store_message": False},
    }

    class ToolQuestion:
        def model_dump(self):
            return {"question": "현재 등록된 계산 로직 알려줘"}

    instance = component.CachedNamedRunFlowTool()
    instance._resolved_chat_input_id = "ChatInput-imported"
    instance._resolved_chat_output_id = "ChatOutput-imported"
    instance._attributes = {"flow_tweak_data": ToolQuestion()}
    assert instance._build_flow_tweak_data() == {
        "ChatInput-imported": {
            "input_value": "현재 등록된 계산 로직 알려줘",
            "should_store_message": False,
        },
        "ChatOutput-imported": {"should_store_message": False},
    }

    try:
        component._question_tweaks("ChatInput-runtime", {"ChatInput_runtime_input_value": "잘못된 키"})
    except ValueError as exc:
        assert "사용자 질문이 비어" in str(exc)
    else:
        raise AssertionError("node-ID 기반 또는 provider 정규화 키를 question으로 허용하면 안 됩니다.")

    question_field = component._question_tool_field()
    assert question_field == {
        "name": "question",
        "display_name": "사용자 질문",
        "info": "현재 사용자 질문 원문입니다.",
        "required": True,
        "value": "",
        "tool_mode": True,
        "type": str,
        "input_types": [],
        "is_list": False,
    }

    synced_outputs = []
    instance.tool_description = "metadata qa"
    instance._sync_flow_outputs = lambda outputs: synced_outputs.extend(outputs)
    instance.get_graph = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Tool schema build must not resolve a child graph")
    )
    description, fields = asyncio.run(instance.get_required_data())
    assert description == "metadata qa"
    assert fields == [question_field]
    assert len(synced_outputs) == 1
    assert synced_outputs[0].kwargs == {
        "name": "lazy_flow_result",
        "display_name": "하위 Flow 결과",
        "method": "_run_selected_flow",
        "types": ["Message", "Data", "Text"],
        "tool_mode": True,
    }

    graph_calls = []
    graph = types.SimpleNamespace(
        vertices=[
            types.SimpleNamespace(
                id="ChatInput-current",
                data={"type": "ChatInput"},
                display_name="Chat Input",
                is_output=False,
                outputs=[],
            ),
            types.SimpleNamespace(
                id="ChatOutput-current",
                data={"type": "ChatOutput"},
                display_name="Chat Output",
                is_output=True,
                outputs=[{"name": "message"}],
            ),
        ],
        successor_map={"ChatInput-current": ["ChatOutput-current"], "ChatOutput-current": []},
    )

    async def fake_get_flow(self, flow_name_selected=None, flow_id_selected=None):
        graph_calls.append(("resolve", flow_name_selected, flow_id_selected))
        return types.SimpleNamespace(data={"id": "current-flow-id", "updated_at": "2026-07-12T10:00:00Z"})

    async def fake_get_graph(self, flow_name_selected=None, flow_id_selected=None, updated_at=None):
        graph_calls.append(("build", flow_name_selected, flow_id_selected, updated_at))
        return graph

    base = component.RunFlowBaseComponent
    monkeypatch.setattr(base, "get_flow", fake_get_flow, raising=False)
    monkeypatch.setattr(base, "get_graph", fake_get_graph, raising=False)
    runtime_instance = component.CachedNamedRunFlowTool()
    runtime_instance._attributes = {}
    assert asyncio.run(runtime_instance.get_graph("Metadata QA", "stale-export-id", None)) is graph
    assert graph_calls == [
        ("resolve", "Metadata QA", None),
        ("build", "Metadata QA", "current-flow-id", "2026-07-12T10:00:00Z"),
    ]
    assert runtime_instance._resolved_chat_input_id == "ChatInput-current"
    assert runtime_instance._resolved_flow_output_target == ("ChatOutput-current", "message")

    graph.vertices[-1].outputs.append({"name": "data"})
    try:
        component._single_graph_output_target(graph)
    except ValueError as exc:
        assert "최종 출력이 정확히 하나" in str(exc)
    else:
        raise AssertionError("두 개 이상의 하위 Flow 출력을 Agent 도구 계약으로 허용하면 안 됩니다.")
    graph.vertices[-1].outputs.pop()

    run_calls = []

    async def fake_run_outputs(*, user_id, output_type):
        assert runtime_instance._last_run_outputs is None
        run_calls.append(("run", user_id, output_type))
        runtime_instance._resolved_flow_output_target = ("ChatOutput-current", "message")
        runtime_instance._last_run_outputs = ["fresh"]
        return runtime_instance._last_run_outputs

    async def fake_resolve(*, vertex_id, output_name):
        run_calls.append(("resolve_output", vertex_id, output_name))
        return "child answer"

    runtime_instance.user_id = "user-1"
    runtime_instance._last_run_outputs = ["stale"]
    runtime_instance._get_cached_run_outputs = fake_run_outputs
    runtime_instance._resolve_flow_output = fake_resolve
    assert asyncio.run(runtime_instance._run_selected_flow()) == "child answer"
    assert run_calls == [
        ("run", "user-1", "any"),
        ("resolve_output", "ChatOutput-current", "message"),
    ]


def test_v5_auxiliary_standalone_flow_exports_are_complete_and_optimized():
    assert not list((ROOT / "flow_exports").glob("dummy_*_flow_v5_standalone.json"))
    exports = {
        "domain": ROOT / "flow_exports" / "domain_saving_flow_v5_standalone.json",
        "table_catalog": ROOT / "flow_exports" / "table_catalog_saving_flow_v5_standalone.json",
        "main_flow_filter": ROOT / "flow_exports" / "main_flow_filter_saving_flow_v5_standalone.json",
        "metadata_qa": ROOT / "flow_exports" / "metadata_qa_flow_v5_standalone.json",
        "router": ROOT / "flow_exports" / "api_router_flow_v5_standalone.json",
        "tool_router": ROOT / "flow_exports" / "agent_tool_router_flow_v5_standalone.json",
    }
    for path in exports.values():
        assert path.exists(), path
        flow = json.loads(path.read_text(encoding="utf-8"))
        assert flow["last_tested_version"] == "1.8.2"
        assert flow["data"]["nodes"]
        assert flow["data"]["edges"]
        node_ids = [node["id"] for node in flow["data"]["nodes"]]
        assert len(node_ids) == len(set(node_ids))
        nodes_by_id = {node["id"]: node for node in flow["data"]["nodes"]}
        for edge in flow["data"]["edges"]:
            target_field = edge["data"]["targetHandle"]["fieldName"]
            target_input = nodes_by_id[edge["target"]]["data"]["node"]["template"][target_field]
            assert target_input.get("advanced") is not True, (
                f"{path.name}: Langflow 1.8.2 removes edge {edge['id']} because "
                f"{edge['target']}.{target_field} is an advanced input"
            )

    for key, path in exports.items():
        if key == "router":
            continue
        flow = json.loads(path.read_text(encoding="utf-8"))
        chat_outputs = [node for node in flow["data"]["nodes"] if node["data"].get("type") == "ChatOutput"]
        assert len(chat_outputs) == 1, key

    for key in ("domain", "table_catalog", "main_flow_filter"):
        flow = json.loads(exports[key].read_text(encoding="utf-8"))
        expected_collection = {
            "domain": "agent_v4_domain_items",
            "table_catalog": "agent_v4_table_catalog_items",
            "main_flow_filter": "agent_v4_main_flow_filters",
        }[key]
        ids = {node["id"] for node in flow["data"]["nodes"]}
        nodes_by_type = {}
        for node in flow["data"]["nodes"]:
            nodes_by_type.setdefault(node["data"].get("type"), []).append(node["id"])
        assert any(node_id.startswith("ExistingLoader-") for node_id in ids)
        assert not any("Refinement" in node_id or node_id.startswith("ReviewGate-") for node_id in ids)
        assert not any(node_id.startswith("WriterDry-") or node_id.startswith("WriterLive-") for node_id in ids)
        assert len([node_id for node_id in ids if node_id.startswith("Writer-")]) == 1
        assert len(nodes_by_type.get("ChatOutput", [])) == 1
        assert len([node_id for node_id in ids if node_id.startswith("Response-")]) == 1
        assert len([node_id for node_id in ids if node_id.startswith("Message-")]) == 1
        loader_id = next(node_id for node_id in ids if node_id.startswith("ExistingLoader-"))
        matcher_id = next(node_id for node_id in ids if node_id.startswith("Matcher-"))
        assert any(
            edge["source"] == loader_id
            and edge["data"]["sourceHandle"]["name"] == "existing_items"
            and edge["target"] == matcher_id
            and edge["data"]["targetHandle"]["fieldName"] == "existing_items"
            for edge in flow["data"]["edges"]
        )
        mongo_nodes = [
            node
            for node in flow["data"]["nodes"]
            if "mongo_database" in node["data"]["node"]["template"]
            and "collection_name" in node["data"]["node"]["template"]
        ]
        assert len(mongo_nodes) == 3
        assert all(node["data"]["node"]["template"]["mongo_database"]["value"] == "datagov" for node in mongo_nodes)
        assert all(
            node["data"]["node"]["template"]["collection_name"]["value"] == expected_collection
            for node in mongo_nodes
        )
        assert all(node["data"]["node"]["template"]["mongo_database"]["load_from_db"] is False for node in mongo_nodes)
        assert all(node["data"]["node"]["template"]["collection_name"]["load_from_db"] is False for node in mongo_nodes)
        assert all(node["data"]["node"]["template"]["mongo_uri"]["value"] == "MONGO_URL" for node in mongo_nodes)
        assert all(node["data"]["node"]["template"]["mongo_uri"]["load_from_db"] is True for node in mongo_nodes)
        assert all(node["data"]["node"]["template"]["mongo_uri"]["advanced"] is False for node in mongo_nodes)

    metadata_qa = json.loads(exports["metadata_qa"].read_text(encoding="utf-8"))
    qa_nodes = {node["id"]: node for node in metadata_qa["data"]["nodes"]}
    qa_edges = {
        (edge["source"], edge["data"]["sourceHandle"]["name"], edge["target"], edge["data"]["targetHandle"]["fieldName"])
        for edge in metadata_qa["data"]["edges"]
    }
    assert len(qa_nodes) == 11
    assert len(metadata_qa["data"]["edges"]) == 18
    assert not {"Loader-domain-metadata-qa", "Loader-table-metadata-qa", "Loader-filter-metadata-qa"}.intersection(qa_nodes)
    snapshot_template = qa_nodes["SnapshotLoader-metadata-qa"]["data"]["node"]["template"]
    assert snapshot_template["mongo_database"]["value"] == "datagov"
    assert snapshot_template["domain_collection_name"]["value"] == "agent_v4_domain_items"
    assert snapshot_template["table_collection_name"]["value"] == "agent_v4_table_catalog_items"
    assert snapshot_template["filter_collection_name"]["value"] == "agent_v4_main_flow_filters"
    assert snapshot_template["cache_ttl_seconds"]["value"] == "15"
    assert snapshot_template["mongo_uri"]["value"] == "MONGO_URL"
    assert snapshot_template["mongo_uri"]["load_from_db"] is True
    assert snapshot_template["mongo_uri"]["advanced"] is False
    assert qa_nodes["Agent-metadata-qa"]["data"]["type"] == "MetadataQaGuardedAgent"
    assert ("Context-metadata-qa", "payload_out", "Agent-metadata-qa", "control_payload") in qa_edges
    assert ("Prompt-metadata-qa", "prompt", "Agent-metadata-qa", "input_value") in qa_edges
    assert ("Agent-metadata-qa", "response", "Normalizer-metadata-qa", "llm_response") in qa_edges
    assert not any("Branch" in node_id or "ExecutionRouter" in node_id or "Attacher" in node_id for node_id in qa_nodes)
    assert len([node for node in qa_nodes.values() if node["data"].get("type") == "ChatOutput"]) == 1

    router = json.loads(exports["router"].read_text(encoding="utf-8"))
    assert len(router["data"]["nodes"]) == 14
    assert len(router["data"]["edges"]) == 13
    smart_router = next(node for node in router["data"]["nodes"] if node["id"] == "SmartRouter-api-router")
    assert len(smart_router["data"]["node"]["outputs"]) == 7
    assert smart_router["data"]["node"]["base_classes"] == []
    callers = [node for node in router["data"]["nodes"] if node["id"].startswith("ApiCaller-")]
    final_gates = [node for node in router["data"]["nodes"] if node["id"].startswith("FinalGate-")]
    assert len(callers) == 5
    assert {node["id"] for node in callers} == {
        "ApiCaller-data_analysis",
        "ApiCaller-metadata_qa",
        "ApiCaller-domain_saving",
        "ApiCaller-table_catalog_saving",
        "ApiCaller-main_flow_filter_saving",
    }
    assert not any("dummy_" in node["id"] for node in router["data"]["nodes"])
    assert not final_gates
    assert all({output["name"] for output in node["data"]["node"]["outputs"]} == {"message", "status_data"} for node in callers)
    assert all("REPLACE_" not in json.dumps(node, ensure_ascii=False) for node in callers)
    assert all(
        node["data"]["node"]["template"]["api_url"]["value"].startswith("/api/v1/run/metadata-driven-v5-")
        for node in callers
    )
    assert all(node["data"]["node"]["template"]["read_timeout_seconds"]["value"] == "240" for node in callers)
    assert all("session_id" in node["data"]["node"]["template"] for node in callers)
    assert not any(node["data"]["node"].get("display_name") == "Run Flow" for node in router["data"]["nodes"])
    chat_input_edges = [edge for edge in router["data"]["edges"] if edge["source"] == "ChatInput-api-router"]
    assert len(chat_input_edges) == 1
    assert chat_input_edges[0]["target"] == "SmartRouter-api-router"
    assert chat_input_edges[0]["data"]["targetHandle"]["fieldName"] == "input_text"
    assert not any(
        edge["data"]["targetHandle"]["fieldName"] == "session_source"
        for edge in router["data"]["edges"]
    )
    for route_name, output_name in (("direct_answer", "category_6_result"), ("clarification", "category_7_result")):
        output_id = f"ChatOutput-{route_name}"
        assert any(
            edge["source"] == "SmartRouter-api-router"
            and edge["data"]["sourceHandle"]["name"] == output_name
            and edge["target"] == output_id
            and edge["data"]["targetHandle"]["fieldName"] == "input_value"
            for edge in router["data"]["edges"]
        )

    tool_router = json.loads(exports["tool_router"].read_text(encoding="utf-8"))
    assert len(tool_router["data"]["nodes"]) == 8
    assert len(tool_router["data"]["edges"]) == 7
    tools = [node for node in tool_router["data"]["nodes"] if node["id"].startswith("CachedFlowTool-")]
    agents = [node for node in tool_router["data"]["nodes"] if node["data"].get("type") == "Agent"]
    outputs = [node for node in tool_router["data"]["nodes"] if node["data"].get("type") == "ChatOutput"]
    assert len(tools) == 5
    assert len(agents) == 1
    assert len(outputs) == 1
    agent_template = agents[0]["data"]["node"]["template"]
    assert agent_template["system_prompt"]["value"] == (
        ROOT / "langflow_components" / "route_flow_v2" / "SYSTEM_PROMPT_KO.md"
    ).read_text(encoding="utf-8")
    assert agent_template["max_iterations"]["value"] == 3
    assert agent_template["n_messages"]["value"] == 6
    assert agent_template["add_current_date_tool"]["value"] is False
    assert agent_template["verbose"]["value"] is False
    assert all(node["data"]["node"]["tool_mode"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["cache_flow"]["value"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["return_direct"]["value"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["flow_id_selected"]["value"] == "" for node in tools)
    assert all("session_source" not in node["data"]["node"]["template"] for node in tools)
    cached_tool_source = (
        ROOT / "langflow_components" / "route_flow_v2" / "01_cached_named_run_flow_tool.py"
    ).read_text(encoding="utf-8")
    assert all(node["data"]["node"]["template"]["code"]["value"] == cached_tool_source for node in tools)
    assert {
        node["data"]["node"]["template"]["tool_name"]["value"] for node in tools
    } == {
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
    }
    edge_keys = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in tool_router["data"]["edges"]
    }
    for tool in tools:
        assert (tool["id"], "component_as_tool", "Agent-agent-tool-router", "tools") in edge_keys
    assert {
        edge for edge in edge_keys if edge[0] == "ChatInput-agent-tool-router"
    } == {("ChatInput-agent-tool-router", "message", "Agent-agent-tool-router", "input_value")}
    assert not any(edge[3] == "session_source" for edge in edge_keys)
    assert ("Agent-agent-tool-router", "response", "ChatOutput-agent-tool-router", "input_value") in edge_keys


def test_flow_tool_entry_inputs_are_agent_controlled():
    specs = [
        ("data_analysis_flow/00_analysis_request_loader.py", "question"),
        ("metadata_qa_flow/00_metadata_qa_request_loader.py", "question"),
        ("domain_saving_flow/00_domain_saving_request_loader.py", "raw_text"),
        ("table_catalog_saving_flow/00_table_catalog_saving_request_loader.py", "raw_text"),
        ("main_flow_filters_saving_flow/00_main_flow_filter_saving_request_loader.py", "raw_text"),
    ]

    for relative_path, input_name in specs:
        module = load_module(ROOT / "langflow_components" / relative_path)
        inputs = _component_inputs(module)
        matching = [item for item in inputs if item.kwargs.get("name") == input_name]
        assert matching, relative_path
        assert matching[0].kwargs.get("tool_mode") is True, relative_path


def test_metadata_saving_duplicate_mode_has_no_non_resumable_ask_option():
    request_paths = [
        "domain_saving_flow/00_domain_saving_request_loader.py",
        "table_catalog_saving_flow/00_table_catalog_saving_request_loader.py",
        "main_flow_filters_saving_flow/00_main_flow_filter_saving_request_loader.py",
    ]
    for relative_path in request_paths:
        module = load_module(ROOT / "langflow_components" / relative_path)
        duplicate_input = next(item for item in _component_inputs(module) if item.kwargs.get("name") == "duplicate_action")
        assert duplicate_input.kwargs.get("value") == "skip"
        assert duplicate_input.kwargs.get("options") == ["skip", "merge", "replace", "create_new"]
        assert module.build_request("metadata", duplicate_action="ask")["request"]["duplicate_action"] == "skip"

    for relative_path in request_paths[:3]:
        module = load_module(ROOT / "langflow_components" / relative_path)
        assert "existing_items" not in [item.kwargs.get("name") for item in _component_inputs(module)]


def test_v5_metadata_candidates_apply_per_pool_policy_for_equipment_uph_question():
    builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    domains = {
        "domain_items": [
            {"section": "process_groups", "key": f"GROUP_{index}", "payload": {"aliases": [f"GROUP {index}"]}}
            for index in range(12)
        ]
        + [{"section": "process_groups", "key": "DA", "payload": {"aliases": ["D/A1"], "description": "D/A process"}}]
    }
    catalogs = {
        "table_catalog_items": [
            {
                "dataset_key": dataset_key,
                "payload": {
                    "description": description,
                    "source_type": "oracle",
                    "source_config": {
                        "db_key": "PNT_RPT",
                        "query_template": "SELECT secret FROM SOURCE_TABLE",
                        "password": "must-not-reach-the-llm",
                    },
                },
            }
            for dataset_key, description in (
                ("production_today", "production data"),
                ("production", "production history"),
                ("wip_today", "wip data"),
                ("wip", "wip history"),
                ("target", "plan data"),
                ("equipment_assign", "equipment assignment"),
                ("eqp_uph", "equipment model UPH"),
                ("lot_status", "lot status"),
                ("hold_history", "hold history"),
            )
        ]
    }
    filters = {
        "main_flow_filters": [
            {"filter_key": f"FILTER_{index:02d}", "payload": {"aliases": [f"filter {index}"]}}
            for index in range(17)
        ]
    }
    result = builder.build_metadata_candidates(
        {"request": {"question": "현재 D/A1 공정에 배정된 장비와 해당 모델의 UPH를 함께 보여줘"}},
        domains,
        catalogs,
        filters,
        max_domain_items=3,
        min_table_items=5,
        max_table_items=5,
        max_bytes=32768,
    )

    candidates = result["metadata_candidates"]
    selected_tables = {item["dataset_key"] for item in candidates["table_catalog_items"]}
    assert len(candidates["domain_items"]) <= 3
    assert any(item["key"] == "DA" for item in candidates["domain_items"])
    assert len(candidates["table_catalog_items"]) == 5
    assert {"equipment_assign", "eqp_uph"}.issubset(selected_tables)
    assert len(candidates["main_flow_filters"]) == 17
    assert result["metadata_load"]["candidate_bytes"] <= 32768
    assert result["metadata_load"]["selection_policy"] == {
        "domain_items": {"mode": "relevant_only", "max_items": 3},
        "table_catalog_items": {"mode": "relevant_with_minimum", "min_items": 5, "max_items": 5},
        "main_flow_filters": {"mode": "all_relevant_first"},
    }
    assert result["metadata_load"]["policy_preserved"] == {
        "table_minimum": True,
        "main_filters_complete": True,
    }
    catalog_text = json.dumps(candidates["table_catalog_items"], ensure_ascii=False)
    assert "query_template" not in catalog_text
    assert "must-not-reach-the-llm" not in catalog_text
    assert "domain_items" not in result


def test_v5_metadata_candidate_byte_fit_trims_domain_before_protected_table_and_filters():
    builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    domains = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": f"DA_{index:02d}",
                "payload": {"aliases": ["D/A1"], "apply_conditions": ["D/A1"], "description": "x" * 1800},
            }
            for index in range(10)
        ]
    }
    catalogs = {
        "table_catalog_items": [
            {"dataset_key": f"table_{index:02d}", "payload": {"description": "table" + "y" * 350}}
            for index in range(6)
        ]
    }
    filters = {
        "main_flow_filters": [
            {"filter_key": f"FILTER_{index:02d}", "payload": {"aliases": [f"filter {index}"], "operator": "eq"}}
            for index in range(17)
        ]
    }

    result = builder.build_metadata_candidates(
        {"request": {"question": "D/A1 기준으로 알려줘"}},
        domains,
        catalogs,
        filters,
        max_domain_items=10,
        min_table_items=5,
        max_table_items=5,
        max_bytes=8192,
    )

    candidates = result["metadata_candidates"]
    metadata_load = result["metadata_load"]
    assert metadata_load["truncated_by_bytes"] is True
    assert metadata_load["byte_trimmed_counts"]["domain_items"] > 0
    assert len(candidates["table_catalog_items"]) == 5
    assert len(candidates["main_flow_filters"]) == 17
    assert metadata_load["candidate_bytes"] <= 8192
    assert metadata_load["policy_preserved"] == {
        "table_minimum": True,
        "main_filters_complete": True,
    }
    assert metadata_load["warnings"] == []


def test_v5_metadata_candidate_byte_fit_reports_forced_policy_truncation():
    builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    result = builder.build_metadata_candidates(
        {"request": {"question": "catalog 확인"}},
        {"domain_items": []},
        {
            "table_catalog_items": [
                {"dataset_key": f"table_{index}", "payload": {"description": "x" * 3000}}
                for index in range(5)
            ]
        },
        {
            "main_flow_filters": [
                {"filter_key": f"FILTER_{index}", "payload": {"description": "y" * 300}}
                for index in range(8)
            ]
        },
        min_table_items=5,
        max_table_items=5,
        max_bytes=4096,
    )

    metadata_load = result["metadata_load"]
    warning_types = {item["type"] for item in metadata_load["warnings"]}
    assert metadata_load["candidate_bytes"] <= 4096
    assert metadata_load["policy_preserved"] == {
        "table_minimum": False,
        "main_filters_complete": False,
    }
    assert warning_types == {
        "table_minimum_unmet_due_to_byte_cap",
        "main_filters_truncated_due_to_byte_cap",
    }


def test_v5_trusted_catalog_hydrator_replaces_llm_source_settings():
    hydrator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py")
    input_names = {item.kwargs.get("name") for item in hydrator.TrustedRetrievalJobHydrator.inputs}
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "wip_today",
                    "source_alias": "wip_data",
                    "source_type": "h_api",
                    "source_config": {"url": "https://untrusted.invalid", "token": "leak"},
                    "required_params": {"DATE": "20260710"},
                }
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    catalog = {
        "table_catalog_items": [
            {
                "dataset_key": "wip_today",
                "payload": {
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                    "source_config": {
                        "db_key": "PNT_RPT",
                        "query_template": "SELECT * FROM WIP WHERE WORK_DATE = {DATE}",
                        "password": "catalog-secret",
                    },
                },
            }
        ]
    }

    result = hydrator.hydrate_retrieval_jobs(payload, catalog, retrieval_mode="live")
    job = result["intent_plan"]["retrieval_jobs"][0]
    assert job["source_type"] == "oracle"
    assert job["source_config"]["db_key"] == "PNT_RPT"
    assert job["source_config"]["query_template"].startswith("SELECT *")
    assert "password" not in job["source_config"]
    assert "untrusted.invalid" not in json.dumps(job)
    assert job["trusted_catalog"] is True
    assert "retrieval_mode" in input_names
    assert "execution_mode" not in input_names
    assert result["request"]["retrieval_mode"] == "live"
    assert result["trace"]["inspection"]["catalog_hydration"]["status"] == "ok"


def test_v5_trusted_catalog_hydrator_blocks_unknown_live_dataset_but_allows_dummy():
    hydrator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py")
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "invented_dataset",
                    "source_alias": "invented",
                    "source_type": "oracle",
                    "source_config": {"query_template": "DROP TABLE X"},
                }
            ]
        },
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }

    live = hydrator.hydrate_retrieval_jobs(payload, {"table_catalog_items": []}, retrieval_mode="live")
    dummy = hydrator.hydrate_retrieval_jobs(payload, {"table_catalog_items": []}, retrieval_mode="dummy")

    assert live["intent_plan"]["retrieval_jobs"] == []
    assert live["trace"]["errors"][0]["type"] == "unknown_dataset_key"
    dummy_job = dummy["intent_plan"]["retrieval_jobs"][0]
    assert dummy_job["dummy_only"] is True
    assert dummy_job["source_type"] == "dummy"
    assert "source_config" not in dummy_job
    validator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "06_retrieval_job_validator.py")
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    validated_dummy = validator.validate_retrieval_payload(dummy)
    dummy_bundle = router.route_retrieval_jobs(validated_dummy, "dummy")
    assert dummy_bundle["retrieval_job_bundle"]["jobs"][0]["dataset_key"] == "invented_dataset"


def test_v5_retrieval_router_emits_only_thin_branch_bundle():
    router = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "07_retrieval_job_router.py")
    payload = {
        "request": {
            "session_id": "s1",
            "reference_date": "20260710",
            "question": "large question",
            "retrieval_mode": "live",
        },
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "wip_today", "source_alias": "wip", "source_type": "oracle"},
                {"dataset_key": "api_data", "source_alias": "api", "source_type": "h_api"},
            ],
            "pandas_execution_plan": [{"step": "large-plan"}],
        },
        "state": {"large": "x" * 5000},
        "runtime_sources": {"old": [{"x": 1}]},
    }

    routed = router.route_retrieval_jobs(payload, "oracle")
    assert set(routed) == {"retrieval_job_bundle", "request_context", "routing_trace"}
    assert routed["retrieval_job_bundle"]["jobs"] == [payload["intent_plan"]["retrieval_jobs"][0]]
    assert routed["request_context"] == {"session_id": "s1", "reference_date": "20260710"}
    assert "intent_plan" not in routed
    assert "state" not in routed
    assert "runtime_sources" not in routed


def test_v5_selected_helper_builder_emits_only_selected_definitions():
    helper_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "15a_selected_helper_code_builder.py")
    library = (ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py").read_text(encoding="utf-8")

    empty = helper_builder.build_selected_helper_code({"available_helpers": []}, library)
    selected = helper_builder.build_selected_helper_code(
        {"available_helpers": [{"function_name": "match_product_tokens"}]},
        library,
    )

    assert empty == ""
    assert "def match_product_tokens" in selected
    assert "def sample_passthrough_helper" not in selected
    assert len(selected.encode("utf-8")) < len(library.encode("utf-8"))


def test_v5_data_analysis_builder_has_one_terminal_path_with_integrated_one_time_repair():
    from tools.build_v5_data_analysis_flow import (
        DEFAULT_SOURCE,
        HELPER_LIBRARY_SOURCE,
        REMOVED_REPAIR_NODES,
        REPAIR_PROMPT_NODE_ID,
        REPAIR_PROMPT_SOURCE,
        build_flow,
    )

    flow = build_flow(DEFAULT_SOURCE)
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edge_keys = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }

    assert not REMOVED_REPAIR_NODES.intersection(nodes)
    assert "CustomComponent-v5RepairGate" not in nodes
    assert nodes["TextInput-AXG9a"]["data"]["node"]["template"]["input_value"]["value"] == (
        HELPER_LIBRARY_SOURCE.read_text(encoding="utf-8")
    )
    assert ("CustomComponent-s3mf1", "payload_out", "CustomComponent-AUrFb", "payload") in edge_keys
    assert ("CustomComponent-v5Helper", "selected_helper_code", "CustomComponent-s3mf1", "function_case_helper_code") in edge_keys
    assert (REPAIR_PROMPT_NODE_ID, "text", "CustomComponent-s3mf1", "repair_prompt_template") in edge_keys
    executor = nodes["CustomComponent-s3mf1"]["data"]["node"]
    assert executor["field_order"] == [
        "payload",
        "llm_response",
        "function_case_helper_code",
        "repair_prompt_template",
        "model",
        "api_key",
        "max_repair_attempts",
    ]
    assert executor["template"]["max_repair_attempts"]["options"] == ["0", "1"]
    assert executor["template"]["max_repair_attempts"]["value"] == "1"
    assert executor["template"]["function_case_helper_code"]["advanced"] is False
    assert executor["template"]["repair_prompt_template"]["advanced"] is False
    assert executor["template"]["repair_prompt_template"]["_input_type"] == "MessageTextInput"
    assert executor["template"]["repair_prompt_template"]["value"] == ""
    repair_prompt_node = nodes[REPAIR_PROMPT_NODE_ID]["data"]["node"]
    assert repair_prompt_node["display_name"] == "17B pandas 복구 프롬프트 템플릿"
    assert repair_prompt_node["template"]["input_value"]["advanced"] is False
    assert repair_prompt_node["template"]["input_value"]["value"] == REPAIR_PROMPT_SOURCE.read_text(encoding="utf-8")
    assert executor["template"]["model"]["value"]
    assert executor["template"]["api_key"]["value"] == "GOOGLE_API_KEY"
    assert "execute_pandas_with_repair" in executor["template"]["code"]["value"]
    assert [node_id for node_id, node in nodes.items() if node["data"].get("type") == "ChatOutput"] == [
        "ChatOutput-rwbTs"
    ]


def test_v5_single_data_analysis_path_keeps_clear_failure_response_fallback():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    payload = {
        "analysis": {
            "status": "error",
            "error": {"type": "pandas_execution_error", "message": "NameError: unknown_column"},
        },
        "data": {"columns": [], "rows": [], "row_count": 0},
        "trace": {
            "warnings": [],
            "errors": [{"type": "pandas_execution_error", "message": "NameError: unknown_column"}],
        },
    }

    answered = answer_builder.build_answer_response(payload, "")

    assert answered["answer_message"] == "분석을 완료하지 못했습니다. trace의 오류를 확인해 주세요."
    assert {notice["type"] for notice in answered["answer_sections"]["notices"]} >= {
        "empty_result",
        "pandas_execution_error",
    }


def test_v5_data_analysis_generated_text_inputs_are_not_dropdowns():
    flow = json.loads((ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json").read_text(encoding="utf-8"))
    node_index = {node["id"]: node for node in flow["data"]["nodes"]}
    generated_nodes = (
        "CustomComponent-DXrpf",
        "CustomComponent-v5Oracle",
        "CustomComponent-v5HApi",
        "CustomComponent-v5Datalake",
        "CustomComponent-v5Goodocs",
        "CustomComponent-v5Helper",
    )
    for node_id in generated_nodes:
        template = node_index[node_id]["data"]["node"]["template"]
        for field_name, field in template.items():
            if field_name in {"_type", "code"} or not isinstance(field, dict) or field.get("type") != "str":
                continue
            assert field.get("_input_type") == "MessageTextInput", (node_id, field_name)
            assert not field.get("options"), (node_id, field_name)


def test_v5_api_contract_has_single_row_code_and_message_owners():
    answer_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "20_answer_response_builder.py")
    api_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "analysis": {"status": "ok", "row_count": 1, "columns": ["QTY"]},
        "data": {"columns": ["QTY"], "rows": [{"QTY": 7}], "row_count": 1},
        "source_results": [{"source_alias": "dummy", "dummy": True, "row_count": 1}],
        "trace": {"warnings": [], "errors": [], "inspection": {"pandas_execution": {"generated_code": "result = df"}}},
    }

    answered = answer_builder.build_answer_response(payload, "dummy result")
    response = api_builder.build_api_response(answered, "### answer\ndummy result")

    assert response["data"]["rows"] == [{"QTY": 7}]
    assert "rows" not in response["analysis"]
    assert "rows" not in response["answer_sections"]["result_table"]
    assert "analysis_code" not in response["analysis"]
    assert response["trace"]["inspection"]["pandas_execution"]["generated_code"] == "result = df"
    assert response["message"].startswith("### answer")
    assert "answer_message" not in response
    assert "display_message" not in response
    assert response["data_mode"] == "dummy"
