from __future__ import annotations

from http import HTTPStatus

from tools import data_ref_download_server as server


def test_data_ref_download_token_round_trip() -> None:
    ref = {
        "store": "mongodb",
        "ref_id": "result:s1:abc",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
        "role": "analysis_result",
        "label": "분석 결과 데이터",
    }

    token = server.encode_data_ref(ref)
    decoded = server.decode_data_ref(token)

    assert decoded == ref
    assert "=" not in token


def test_data_ref_download_query_supports_direct_ref_params() -> None:
    ref = server.data_ref_from_query(
        "ref_id=result%3As1%3Aabc&path=payload.runtime_sources.production_data&source_alias=production_data"
    )

    assert ref == {
        "store": "mongodb",
        "ref_id": "result:s1:abc",
        "path": "payload.runtime_sources.production_data",
        "source_alias": "production_data",
    }


def test_data_ref_download_resolve_reports_bad_token_without_mongo_call() -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://unused",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )

    resolved = server.resolve_request("download_ref=not-valid-base64", config, limit=10)

    assert resolved["ok"] is False
    assert "토큰 해석 실패" in resolved["message"]


def test_data_ref_download_resolve_reports_expired_ref_as_gone(monkeypatch) -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )

    def fake_load_data_ref_rows(*args, **kwargs):
        return {"ok": False, "expired": True, "message": "data_ref expired.", "rows": []}

    monkeypatch.setattr(server, "load_data_ref_rows", fake_load_data_ref_rows)
    token = server.encode_data_ref({"store": "mongodb", "ref_id": "result:s1:expired"})

    resolved = server.resolve_request(f"download_ref={token}", config, limit=10)

    assert resolved["ok"] is False
    assert resolved["status"] == HTTPStatus.GONE


def test_data_ref_download_csv_uses_utf8_bom_and_headers() -> None:
    payload = server.rows_to_csv_bytes(
        [{"DEVICE": "DEV-A", "생산량": 123}],
        ["DEVICE", "생산량"],
    )

    assert payload.startswith("\ufeff".encode("utf-8"))
    text = payload.decode("utf-8-sig")
    assert "DEVICE,생산량" in text
    assert "DEV-A,123" in text
