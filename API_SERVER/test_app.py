from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from API_SERVER.app import _csv_size_within_limit, _iter_csv_bytes, create_app
from API_SERVER.support import ServerConfig


@dataclass
class _DeleteResult:
    deleted_count: int


class FakeReportCollection:
    """Small in-memory MongoDB collection sufficient for API integration tests."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    def create_index(self, *_: Any, **__: Any) -> str:
        return "index"

    def insert_one(self, document: dict[str, Any]) -> object:
        report_id = document.get("report_id")
        if any(item.get("report_id") == report_id for item in self.documents):
            raise RuntimeError("duplicate report_id")
        stored = deepcopy(document)
        stored.setdefault("_id", f"report-{len(self.documents) + 1}")
        self.documents.append(stored)
        return object()

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        document = self._first(query)
        return deepcopy(document) if document else None

    def find_one_and_delete(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                return deepcopy(self.documents.pop(index))
        return None

    def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                self.documents.pop(index)
                return _DeleteResult(deleted_count=1)
        return _DeleteResult(deleted_count=0)

    def find(
        self,
        query: dict[str, Any],
        _projection: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self.documents if self._matches(item, query)]

    def _first(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next((item for item in self.documents if self._matches(item, query)), None)

    @staticmethod
    def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            value = document.get(key)
            if isinstance(expected, dict) and "$lte" in expected:
                if value is None or value > expected["$lte"]:
                    return False
            elif value != expected:
                return False
        return True


class FakeReportDatabase:
    def __init__(self, cluster: "FakeReportCluster", name: str) -> None:
        self.cluster = cluster
        self.name = name

    def __getitem__(self, _collection_name: str) -> FakeReportCollection:
        return self.cluster.reports


class FakeReportClient:
    def __init__(self, cluster: "FakeReportCluster") -> None:
        self.cluster = cluster
        self.admin = self

    def command(self, command: str) -> dict[str, int]:
        assert command == "ping"
        return {"ok": 1}

    def __getitem__(self, name: str) -> FakeReportDatabase:
        return FakeReportDatabase(self.cluster, name)

    def close(self) -> None:
        return None


class FakeReportCluster:
    def __init__(self) -> None:
        self.reports = FakeReportCollection()

    def mongo_client(self, *_: Any, **__: Any) -> FakeReportClient:
        return FakeReportClient(self)


def install_fake_report_store(monkeypatch) -> FakeReportCluster:
    cluster = FakeReportCluster()
    monkeypatch.setattr("API_SERVER.report_store.MongoClient", cluster.mongo_client)
    return cluster


def make_config() -> ServerConfig:
    return ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        host="0.0.0.0",
        port=5000,
        report_mongo_uri="mongodb://report-test",
        report_database="report_test",
        report_collection="report_save_test",
        report_base_url="http://aaa.test.com",
        use_report_access_token=True,
    )


def test_default_report_storage_name_does_not_use_artifact_prefix() -> None:
    config = ServerConfig(
        mongo_uri="mongodb://report-test",
        mongo_database="report_test",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        report_base_url="http://aaa.test.com",
    )

    assert config.report_collection == "report_save_db"


def test_root_redirects_to_docs_and_hello_is_not_exposed(monkeypatch) -> None:
    install_fake_report_store(monkeypatch)
    with TestClient(create_app(make_config())) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/docs"

        docs = client.get("/docs")
        assert docs.status_code == 200

        removed_example = client.get("/hello")
        assert removed_example.status_code == 404


def test_report_lifecycle_uses_one_mongodb_collection(monkeypatch) -> None:
    cluster = install_fake_report_store(monkeypatch)
    with TestClient(create_app(make_config())) as client:
        live = client.get("/live")
        assert live.status_code == 200
        assert live.json()["service"] == "metadata-driven-v5-artifact-api-server"

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["report_base_url"] == "http://aaa.test.com"
        assert health.json()["report_storage"]["backend"] == "mongodb_collection"

        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {
            "data_ref_mongo": False,
            "report_storage_mongo": True,
        }

        created = client.post(
            "/reports",
            json={"html": "<html><body>normal</body></html>", "title": "Report"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["view_url"].startswith("http://aaa.test.com/reports/view/")
        assert "?token=" in body["view_url"]
        assert body["storage"] == {
            "backend": "mongodb_collection",
            "database": "report_test",
            "collection": "report_save_test",
        }
        assert len(cluster.reports.documents) == 1
        stored_report = cluster.reports.documents[0]
        assert stored_report["storage_backend"] == "mongodb_collection"
        assert stored_report["html"] == "<html><body>normal</body></html>"

        view_path = body["view_url"].removeprefix("http://aaa.test.com")
        view = client.get(view_path)
        assert view.status_code == 200
        assert "normal" in view.text
        assert "content-security-policy" in view.headers

        download_path = body["download_url"].removeprefix("http://aaa.test.com")
        download = client.get(download_path)
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")

        token = body["view_url"].split("?token=", 1)[1]
        deleted = client.delete(f"/reports/{body['report_id']}?token={token}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert cluster.reports.documents == []


def test_csv_stream_is_bom_prefixed_and_enforces_size_limit() -> None:
    rows = [{"name": "A", "count": 2}, {"name": "B", "count": 3}]
    payload = b"".join(_iter_csv_bytes(rows, ["name", "count"]))
    assert payload.startswith(b"\xef\xbb\xbfname,count\r\n")
    assert _csv_size_within_limit(rows, ["name", "count"], len(payload)) == len(payload)
    assert _csv_size_within_limit(rows, ["name", "count"], len(payload) - 1) is None
