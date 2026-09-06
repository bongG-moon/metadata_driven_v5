"""Tests for the deliberately small employee-directory replacement utility."""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import employee_directory_import as importer


class FakeCollection:
    """Capture replacement calls without connecting to MongoDB."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = [{"empno": "old"}]
        self.operations: list[tuple[str, Any]] = []

    def delete_many(self, selector: dict[str, Any]) -> None:
        self.operations.append(("delete_many", copy.deepcopy(selector)))
        self.documents = []

    def insert_many(self, documents, *args, **kwargs) -> None:
        del args, kwargs
        copied = [copy.deepcopy(dict(document)) for document in documents]
        self.operations.append(("insert_many", copied))
        self.documents.extend(copied)


class FakeDatabase:
    def __init__(self, collection: FakeCollection) -> None:
        self.collection = collection

    def __getitem__(self, _name: str) -> FakeCollection:
        return self.collection


class FakeMongoClient:
    """A small module-level MongoClient replacement for public API tests."""

    instances: list["FakeMongoClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.collection = FakeCollection()
        self.closed = False
        type(self).instances.append(self)

    def __getitem__(self, _name: str) -> FakeDatabase:
        return FakeDatabase(self.collection)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_mongo_client() -> None:
    FakeMongoClient.instances = []


def _employee_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "empno": "2069026",
                "emp_nm": "문봉건",
                "dept_nm": "PKG",
                "unrelated_source_column": "저장하면 안 됨",
            },
            {
                "empno": "2071044",
                "emp_nm": "최은서",
                "dept_nm": "PKG 생산기술",
                "unrelated_source_column": "저장하면 안 됨",
            },
        ]
    )


@pytest.mark.parametrize("missing_column", ["empno", "emp_nm", "dept_nm"])
def test_replace_employee_directory_requires_all_three_dataframe_columns(
    missing_column: str,
) -> None:
    dataframe = _employee_frame().drop(columns=[missing_column])

    with pytest.raises(ValueError):
        importer.replace_employee_directory(
            dataframe,
            mongo_uri="mongodb://unit-test",
            database="portal_test",
            collection="portal_employee_directory",
        )


def test_empty_dataframe_does_not_open_or_change_mongodb(monkeypatch) -> None:
    monkeypatch.setattr(importer, "MongoClient", FakeMongoClient)
    empty_dataframe = pd.DataFrame(columns=["empno", "emp_nm", "dept_nm"])

    saved_count = importer.replace_employee_directory(
        empty_dataframe,
        mongo_uri="mongodb://unit-test",
        database="portal_test",
        collection="portal_employee_directory",
    )

    assert saved_count == 0
    assert FakeMongoClient.instances == []


def test_nonempty_dataframe_replaces_collection_with_exact_three_key_documents(
    monkeypatch,
) -> None:
    monkeypatch.setattr(importer, "MongoClient", FakeMongoClient)

    saved_count = importer.replace_employee_directory(
        _employee_frame(),
        mongo_uri="mongodb://unit-test",
        database="portal_test",
        collection="portal_employee_directory",
    )

    assert saved_count == 2
    assert len(FakeMongoClient.instances) == 1
    client = FakeMongoClient.instances[0]
    assert client.closed is True
    assert client.collection.operations == [
        ("delete_many", {}),
        (
            "insert_many",
            [
                {"empno": "2069026", "emp_nm": "문봉건", "dept_nm": "PKG"},
                {
                    "empno": "2071044",
                    "emp_nm": "최은서",
                    "dept_nm": "PKG 생산기술",
                },
            ],
        ),
    ]
    assert all(
        set(document) == {"empno", "emp_nm", "dept_nm"}
        for document in client.collection.documents
    )


class _EmpnoOnlyLookupCollection:
    """Return a record only when Portal really queries ``empno``."""

    def __init__(self) -> None:
        self.selector: dict[str, Any] | None = None
        self.projection: dict[str, Any] | None = None

    def find_one(
        self,
        selector: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, str] | None:
        self.selector = copy.deepcopy(selector)
        self.projection = copy.deepcopy(projection)
        candidates = selector.get("$or", [selector])
        if any(candidate.get("empno") == "2069026" for candidate in candidates):
            return {"empno": "2069026", "emp_nm": "문봉건", "dept_nm": "PKG"}
        return None


def test_portal_employee_lookup_reads_empno_and_emp_nm(monkeypatch) -> None:
    # Do not load the Portal while pytest is collecting the schedule contract
    # module: that module explicitly selects its isolated ``test`` adapter.
    monkeypatch.setenv("PTMORE_PORTAL_AUTH_MODE", "test")
    portal_app = importlib.import_module("app")
    collection = _EmpnoOnlyLookupCollection()
    # Construct the reader without opening a real MongoDB client.  The public
    # method still exercises the actual selector and result-field handling.
    store = object.__new__(portal_app.MongoEmployeeDirectoryStore)
    store._collection = collection
    store._client = None
    store._mongo_error = Exception

    assert store.resolve_name("2069026") == "문봉건"
    assert collection.selector is not None
    candidates = collection.selector.get("$or", [collection.selector])
    assert {"empno": "2069026"} in candidates
    assert collection.projection is not None
    assert collection.projection.get("emp_nm") == 1
