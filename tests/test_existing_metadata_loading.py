from __future__ import annotations

import importlib.util
from importlib.machinery import ModuleSpec
import sys
import types
from copy import deepcopy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _install_lfx_stubs() -> None:
    if "lfx" in sys.modules or importlib.util.find_spec("lfx") is not None:
        return

    class Component:
        pass

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

    modules = {
        "lfx": types.ModuleType("lfx"),
        "lfx.custom": types.ModuleType("lfx.custom"),
        "lfx.custom.custom_component": types.ModuleType("lfx.custom.custom_component"),
        "lfx.custom.custom_component.component": types.ModuleType("lfx.custom.custom_component.component"),
        "lfx.io": types.ModuleType("lfx.io"),
        "lfx.schema": types.ModuleType("lfx.schema"),
        "lfx.schema.data": types.ModuleType("lfx.schema.data"),
        "lfx.schema.message": types.ModuleType("lfx.schema.message"),
    }
    modules["lfx"].__spec__ = ModuleSpec("lfx", loader=None)
    modules["lfx.custom.custom_component.component"].Component = Component
    modules["lfx.io"].BoolInput = BoolInput
    modules["lfx.io"].DataInput = InputBase
    modules["lfx.io"].DropdownInput = InputBase
    modules["lfx.io"].MessageInput = InputBase
    modules["lfx.io"].MessageTextInput = InputBase
    modules["lfx.io"].ModelInput = InputBase
    modules["lfx.io"].MultilineInput = InputBase
    modules["lfx.io"].Output = InputBase
    modules["lfx.io"].SecretStrInput = SecretStrInput
    modules["lfx.schema.data"].Data = Data
    modules["lfx.schema.message"].Message = Message
    for name, module in modules.items():
        sys.modules[name] = module


_install_lfx_stubs()


def _load_module(path: Path):
    module_name = f"_metadata_loading_{path.parent.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _install_fake_pymongo(monkeypatch):
    store: dict[str, dict[str, dict[str, dict]]] = {}
    find_one_queries = []

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
            docs = []
            for doc in self.docs.values():
                projected = deepcopy(doc)
                for key, included in (projection or {}).items():
                    if included == 0:
                        projected.pop(key, None)
                docs.append(projected)
            return FakeCursor(docs)

        def find_one(self, query=None, projection=None):
            query = query or {}
            find_one_queries.append(deepcopy(query))
            doc = self.docs.get(query.get("_id"))
            return deepcopy(doc) if doc is not None else None

    class FakeDatabase:
        def __init__(self, collections):
            self.collections = collections

        def __getitem__(self, collection_name):
            return FakeCollection(self.collections.setdefault(collection_name, {}))

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, database_name):
            return FakeDatabase(store.setdefault(database_name, {}))

        def close(self):
            pass

    pymongo = types.ModuleType("pymongo")
    pymongo.MongoClient = FakeClient
    monkeypatch.setitem(sys.modules, "pymongo", pymongo)
    return store, find_one_queries


SPECS = [
    (
        "domain_saving_flow/00_domain_existing_items_loader.py",
        "domain_saving_flow/05_domain_similarity_checker.py",
        "agent_v4_domain_items",
        "domain:process_groups:DA",
        "domain:process_groups:WB",
        {"section": "process_groups", "key": "DA"},
        {"section": "process_groups", "key": "WB"},
    ),
    (
        "table_catalog_saving_flow/00_table_catalog_existing_items_loader.py",
        "table_catalog_saving_flow/05_table_catalog_similarity_checker.py",
        "agent_v4_table_catalog_items",
        "table_catalog:wip_today",
        "table_catalog:target",
        {"dataset_key": "wip_today"},
        {"dataset_key": "target"},
    ),
    (
        "main_flow_filters_saving_flow/00_main_flow_filter_existing_items_loader.py",
        "main_flow_filters_saving_flow/05_main_flow_filter_similarity_checker.py",
        "agent_v4_main_flow_filters",
        "main_flow_filter:DATE",
        "main_flow_filter:OPER",
        {"filter_key": "DATE"},
        {"filter_key": "OPER"},
    ),
]


@pytest.mark.parametrize("loader_path,_matcher_path,collection,doc_id,_missing_id,key_fields,_missing_fields", SPECS)
def test_existing_loader_keeps_full_document_without_registration_trace(
    monkeypatch, loader_path, _matcher_path, collection, doc_id, _missing_id, key_fields, _missing_fields
):
    store, _queries = _install_fake_pymongo(monkeypatch)
    store["datagov"] = {
        collection: {
            doc_id: {
                "_id": doc_id,
                **key_fields,
                "status": "active",
                "payload": {"display_name": "retained", "nested": {"value": 1}},
                "registration_trace": {"raw_text": "must-not-propagate"},
            }
        }
    }
    loader = _load_module(ROOT / "langflow_components" / loader_path)

    result = loader.load_existing_items("mongodb://fake", "datagov", collection)

    assert result["existing_items"][0]["payload"]["nested"]["value"] == 1
    assert "registration_trace" not in result["existing_items"][0]


@pytest.mark.parametrize("_loader_path,matcher_path,collection,doc_id,missing_id,key_fields,missing_fields", SPECS)
def test_matcher_exact_queries_candidates_missing_from_provided_loader_window(
    monkeypatch, _loader_path, matcher_path, collection, doc_id, missing_id, key_fields, missing_fields
):
    store, queries = _install_fake_pymongo(monkeypatch)
    provided = {"_id": doc_id, **key_fields, "status": "active", "payload": {"source": "loader", "keep": True}}
    outside_loader_window = {
        "_id": missing_id,
        **missing_fields,
        "status": "active",
        "payload": {"source": "exact_lookup", "keep": True},
    }
    store["datagov"] = {collection: {doc_id: provided, missing_id: outside_loader_window}}
    matcher = _load_module(ROOT / "langflow_components" / matcher_path)
    payload = {"items": [{**key_fields, "payload": {}}, {**missing_fields, "payload": {}}]}

    result = matcher.check_similarity(
        payload,
        {"existing_items": [provided]},
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name=collection,
    )

    assert {match["existing_item"]["payload"]["source"] for match in result["existing_matches"]} == {
        "loader",
        "exact_lookup",
    }
    assert queries == [{"_id": missing_id}]
    assert result["trace"]["duplicate_lookup"]["provided_count"] == 1
    assert result["trace"]["duplicate_lookup"]["queried_candidate_count"] == 1
    assert result["trace"]["duplicate_lookup"]["loaded_count"] == 1
    assert result["trace"]["duplicate_lookup"]["count"] == 2


def test_domain_matcher_targeted_identity_query_finds_alias_outside_loader_window(monkeypatch):
    store, queries = _install_fake_pymongo(monkeypatch)
    existing = {
        "_id": "domain:process_groups:BG",
        "section": "process_groups",
        "key": "BG",
        "status": "active",
        "payload": {"display_name": "BG", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2"]},
    }
    store["datagov"] = {"agent_v4_domain_items": {existing["_id"]: existing}}
    matcher = _load_module(ROOT / "langflow_components" / "domain_saving_flow" / "05_domain_similarity_checker.py")
    payload = {
        "items": [
            {
                "section": "process_groups",
                "key": "BG_PROCESS_GROUP",
                "payload": {"display_name": "BG 공정 그룹", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2", "B/G3"]},
            }
        ]
    }

    result = matcher.check_similarity(
        payload,
        {"existing_items": []},
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="agent_v4_domain_items",
    )

    assert queries == [{"_id": "domain:process_groups:BG_PROCESS_GROUP"}]
    assert result["existing_matches"][0]["existing_key"] == "process_groups:BG"
    assert result["existing_matches"][0]["match_type"] == "identity_overlap"
    assert result["trace"]["duplicate_lookup"]["identity_query_count"] == 1
    assert result["trace"]["duplicate_lookup"]["loaded_count"] == 1
