from collections import defaultdict
from pathlib import Path
import sys

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as portal_app


application = portal_app.application


client = TestClient(application)


def _active_user_count(
    usage_history: list[dict[str, str]],
    *,
    min_distinct_days: int,
    min_chat_count: int,
) -> int:
    chat_counts: dict[str, int] = defaultdict(int)
    active_dates: dict[str, set[str]] = defaultdict(set)
    for record in usage_history:
        employee_id = record["employee_id"]
        chat_counts[employee_id] += 1
        active_dates[employee_id].add(record["date"])

    return sum(
        chat_counts[employee_id] >= min_chat_count
        and len(active_dates[employee_id]) >= min_distinct_days
        for employee_id in chat_counts
    )


def _kpi_number(value: object) -> int:
    return int(str(value).replace(",", "").replace("명", "").strip())


def test_design_preview_routes_are_available() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "PTMORE PKG Agent Portal" in response.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_dummy_portal_contract_has_all_design_sections() -> None:
    response = client.get("/api/mock/portal")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "viewer",
        "dashboard",
        "schedules",
        "metadata",
        "metadata_authoring",
        "settings",
        "usage_history",
    }
    assert payload["viewer"]["is_admin"] is True
    assert len(payload["schedules"]) >= 1
    assert set(payload["metadata"]) == {"table_catalog", "main_flow_filters", "domain"}
    assert payload["metadata"]["table_catalog"]
    assert payload["metadata"]["main_flow_filters"]
    assert payload["metadata"]["domain"]
    assert len(payload["settings"]["admins"]) >= 1
    assert payload["settings"]["access_policy"] == {
        "metadata_page": "admin_only",
        "metadata_registration": "admin_only",
        "schedule_update": "owner_or_admin",
        "all_schedule_view": "all_users",
    }

    first_catalog = payload["metadata"]["table_catalog"][0]
    assert {"dataset_key", "display_name", "source_type", "required_filters"} <= first_catalog.keys()
    first_filter = payload["metadata"]["main_flow_filters"][0]
    assert {"filter_key", "semantic_role", "column_candidates", "aliases"} <= first_filter.keys()
    first_domain = payload["metadata"]["domain"][0]
    assert {"section", "key", "aliases", "question_cues"} <= first_domain.keys()
    assert all({"repeat", "time", "owner"} <= schedule.keys() for schedule in payload["schedules"])

    usage_history = payload["usage_history"]
    assert isinstance(usage_history, list)
    assert usage_history
    assert all(
        {"employee_id", "question", "date"} <= record.keys()
        for record in usage_history
    )


def test_metadata_authoring_preview_matches_rev2_flow_contract() -> None:
    portal_payload = client.get("/api/mock/portal").json()
    response = client.get("/api/mock/metadata-authoring")

    assert response.status_code == 200
    authoring = response.json()
    assert authoring == portal_payload["metadata_authoring"]
    assert authoring["contract"] == {
        "version": "metadata_authoring.rev_2.v1",
        "request": {
            "chat_input": "input_value",
            "request_loader": ["raw_text", "duplicate_action", "dry_run"],
            "defaults": {"duplicate_action": "skip", "dry_run": True},
        },
        "response": [
            "status",
            "data.columns/data.rows",
            "metadata_authoring",
            "write_result",
            "trace",
        ],
    }

    assert set(authoring["examples"]) == {"table_catalog", "main_flow_filters", "domain"}
    for example in authoring["examples"].values():
        assert example["raw_text"]
        assert example["duplicate_action"] in {"skip", "merge", "replace", "create_new"}
        assert example["dry_run"] is True
        result = example["result"]
        assert result["response_type"] == "metadata_authoring"
        assert result["status"] == "dry_run"
        assert result["data"]["row_count"] == len(result["data"]["rows"])
        assert result["metadata_authoring"]["contract_validation"]["status"] == "validated"
        assert result["write_result"]["dry_run"] is True


def test_dashboard_usage_history_uses_active_user_policy() -> None:
    payload = client.get("/api/mock/portal").json()
    dashboard = payload["dashboard"]
    usage_history = payload["usage_history"]
    usage_policy = payload["settings"]["usage_policy"]

    assert len(dashboard["usage_by_day"]) == 21
    assert len(dashboard["kpis"]) == 5
    kpis_by_label = {kpi["label"]: kpi for kpi in dashboard["kpis"]}
    assert set(kpis_by_label) == {
        "일 평균 사용자",
        "일 평균 채팅",
        "누적 사용자",
        "누적 채팅",
        "활성 사용자",
    }

    min_distinct_days = usage_policy["active_user_min_distinct_days"]
    min_chat_count = usage_policy["active_user_min_chat_count"]
    assert min_distinct_days >= 1
    assert min_chat_count >= 1
    assert dashboard["active_user_rule"] == {
        "min_distinct_days": min_distinct_days,
        "min_chat_count": min_chat_count,
    }

    expected_active_users = _active_user_count(
        usage_history,
        min_distinct_days=min_distinct_days,
        min_chat_count=min_chat_count,
    )
    assert dashboard["active_user_count"] == expected_active_users
    assert _kpi_number(kpis_by_label["활성 사용자"]["value"]) == expected_active_users


def test_usage_history_endpoint_matches_portal_contract() -> None:
    portal_payload = client.get("/api/mock/portal").json()
    response = client.get("/api/mock/usage-history")

    assert response.status_code == 200
    assert response.json() == portal_payload["usage_history"]


def test_standard_user_preview_exposes_own_schedule_scope_data() -> None:
    response = client.get("/api/mock/portal?preview_role=user")

    assert response.status_code == 200
    payload = response.json()
    assert payload["viewer"] == {
        "employee_id": "2071044",
        "name": "김민서",
        "role": "일반 사용자",
        "is_admin": False,
    }
    assert any(schedule["owner"] == payload["viewer"]["employee_id"] for schedule in payload["schedules"])


def test_health_describes_dummy_preview_mode() -> None:
    assert client.get("/health").json() == {"status": "ok", "mode": "dummy-preview"}


def test_metadata_authoring_status_defaults_to_safe_preview_mode(monkeypatch) -> None:
    monkeypatch.delenv("PTMORE_METADATA_API_MODE", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_API_URL", raising=False)

    response = client.get("/api/metadata-authoring/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "preview"
    assert payload["ready"] is True
    assert payload["preview_only"] is True
    assert payload["api"]["endpoint_configured"] == {
        "table_catalog": False,
        "main_flow_filters": False,
        "domain": False,
    }
    assert payload["mongodb"]["uri_configured"] is False


def test_metadata_authoring_preview_requires_explicit_preview_mode(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "preview")

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "domain",
            "raw_text": "section은 process_groups이고 key는 DA입니다.",
            "duplicate_action": "merge",
            "dry_run": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"].startswith("META-")
    assert payload["preview_only"] is True
    assert payload["requested_dry_run"] is False
    assert payload["response"]["metadata_authoring"]["original_text"].startswith("section은")
    assert payload["response"]["metadata_authoring"]["duplicate_action"] == "merge"
    assert payload["response"]["trace"]["mode"] == "preview"


def test_metadata_authoring_api_calls_external_flow_and_normalizes_result(monkeypatch) -> None:
    class FakeMetadataApiClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def post_json(self, url, *, payload, headers, timeout_seconds, verify_tls):
            self.calls.append(
                {
                    "url": url,
                    "payload": dict(payload),
                    "headers": dict(headers),
                    "timeout_seconds": timeout_seconds,
                    "verify_tls": verify_tls,
                }
            )
            return {
                "outputs": [
                    {
                        "outputs": [
                            {
                                "component_id": "Api-table_catalog-rev-2",
                                "component_display_name": "10 테이블 카탈로그 등록 API 응답 생성기",
                                "results": {
                                    "api_response": {
                                        "data": {
                                            "response_type": "metadata_authoring",
                                            "metadata_type": "table_catalog",
                                            "status": "saved",
                                            "success": True,
                                            "data": {
                                                "columns": ["데이터셋 키"],
                                                "rows": [{"데이터셋 키": "production_weekly"}],
                                            },
                                            "metadata_authoring": {
                                                "contract_validation": {"status": "validated"}
                                            },
                                            "write_result": {
                                                "success": True,
                                                "saved_count": 1,
                                                "status": "saved",
                                            },
                                        }
                                    }
                                }
                            }
                        ]
                    }
                ]
            }

    fake_client = FakeMetadataApiClient()
    monkeypatch.setattr(portal_app, "_metadata_http_client", fake_client)
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("PTMORE_METADATA_API_URL", "https://metadata.example.test/api/v1/run/flow")
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_HEADER", "X-API-Key")
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_KEY", "test-api-key")
    monkeypatch.setenv("PTMORE_METADATA_API_USER_ID", "2069026")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_COLLECTION_PREFIX", "agent_v4_")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "true")

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "replace",
            "dry_run": False,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["preview_only"] is False
    assert result["requested_dry_run"] is False
    assert result["response"]["status"] == "saved"
    assert result["response"]["data"]["row_count"] == 1
    assert result["response"]["metadata_authoring"]["original_text"] == "dataset_key는 production_weekly입니다."

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["url"] == "https://metadata.example.test/api/v1/run/flow"
    assert call["headers"]["X-API-Key"] == "test-api-key"
    assert call["headers"]["X-Gaia-User-Id"] == "2069026"
    outbound_payload = call["payload"]
    assert outbound_payload["input_value"] == "dataset_key는 production_weekly입니다."
    assert outbound_payload["output_type"] == "any"
    tweaks = outbound_payload["tweaks"]
    assert tweaks["00 테이블 카탈로그 등록 요청 로더"] == {
        "duplicate_action": "replace",
        "dry_run": False,
    }
    assert tweaks["07 테이블 카탈로그 검수/저장 처리기"] == {
        "mongo_uri": "mongodb://example.test:27017",
        "mongo_database": "datagov",
        "collection_name": "agent_v4_table_catalog_items",
    }
    assert tweaks["01 메타데이터 QA 통합 Snapshot 로더"] == {
        "mongo_uri": "mongodb://example.test:27017",
        "mongo_database": "datagov",
        "table_collection_name": "agent_v4_table_catalog_items",
        "filter_collection_name": "agent_v4_main_flow_filters",
        "domain_collection_name": "agent_v4_domain_items",
    }


def test_metadata_authoring_api_mode_does_not_fallback_when_endpoint_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.delenv("PTMORE_METADATA_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_TABLE_CATALOG_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_DOMAIN_API_URL", raising=False)

    status_response = client.get("/api/metadata-authoring/status")
    assert status_response.status_code == 200
    assert status_response.json()["ready"] is False
    assert "endpoint:table_catalog" in status_response.json()["missing"]

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "metadata_api_not_ready"


def test_metadata_authoring_allows_a_type_specific_endpoint(monkeypatch) -> None:
    class FakeMetadataApiClient:
        def post_json(self, url, *, payload, headers, timeout_seconds, verify_tls):
            return {
                "api_response": {
                    "response_type": "metadata_authoring",
                    "status": "dry_run",
                    "success": True,
                    "metadata_authoring": {},
                    "write_result": {"success": True, "status": "dry_run"},
                }
            }

    monkeypatch.setattr(portal_app, "_metadata_http_client", FakeMetadataApiClient())
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.delenv("PTMORE_METADATA_API_URL", raising=False)
    monkeypatch.setenv(
        "PTMORE_METADATA_DOMAIN_API_URL",
        "https://metadata.example.test/api/v1/run/domain-flow",
    )
    monkeypatch.delenv("PTMORE_METADATA_TABLE_CATALOG_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL", raising=False)

    status_response = client.get("/api/metadata-authoring/status")
    assert status_response.json()["ready"] is True
    assert status_response.json()["all_metadata_types_ready"] is False

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "domain",
            "raw_text": "section은 process_groups이고 key는 DA입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["preview_only"] is False


def test_metadata_mongo_values_are_not_sent_without_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("MONGODB_URI", "mongodb://sensitive.example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "false")

    settings = portal_app._metadata_settings_from_env()
    payload = portal_app._metadata_api_payload(
        settings,
        metadata_type="domain",
        raw_text="section은 process_groups이고 key는 DA입니다.",
        duplicate_action="skip",
        dry_run=True,
    )

    assert payload["tweaks"] == {
        "00 도메인 등록 요청 로더": {
            "duplicate_action": "skip",
            "dry_run": True,
        }
    }


def test_metadata_mongo_tweak_mode_requires_configured_mongo_values(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("PTMORE_METADATA_API_URL", "https://metadata.example.test/api/v1/run/flow")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "true")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    status_response = client.get("/api/metadata-authoring/status")
    status_payload = status_response.json()
    assert status_payload["ready"] is False
    assert "MONGODB_URI" in status_payload["missing"]
    assert "MONGODB_DATABASE" in status_payload["missing"]

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "metadata_api_not_ready"
