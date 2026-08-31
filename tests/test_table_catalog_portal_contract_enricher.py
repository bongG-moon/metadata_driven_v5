from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


ENRICHER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "08a_table_catalog_portal_contract_enricher.py"
)


def _response(*, status: str = "dry_run", message: str = "기존 Writer 결과입니다.") -> dict:
    return {
        "response_type": "metadata_authoring",
        "metadata_type": "table_catalog",
        "metadata_label": "테이블 카탈로그",
        "status": status,
        "success": status in {"saved", "dry_run"},
        "direct_response_ready": True,
        "message": message,
        "answer_sections": {
            "summary": {"headline": message, "description": message},
            "key_points": ["기존 응답"],
            "notices": [{"type": "info", "title": "기존", "message": "기존 notice"}],
            "next_steps": ["기존 다음 단계"],
        },
        "data": {
            "columns": ["데이터셋 키"],
            "rows": [{"데이터셋 키": "equipment_assign"}],
            "row_count": 1,
        },
        "metadata_authoring": {
            "generated_count": 1,
            "saved_count": 0,
            "would_save_count": 1,
            "existing_match_count": 0,
            "dry_run": True,
            "keys": ["equipment_assign"],
        },
        "write_result": {
            "success": status in {"saved", "dry_run"},
            "ready_to_save": status in {"saved", "dry_run"},
            "dry_run": status == "dry_run",
            "saved_count": 0,
            "would_save_count": 1,
            "database": "datagov",
            "collection_name": "table_catalog",
            "keys": ["equipment_assign"],
            "operation_by_key": [{"key": "equipment_assign", "operation": "would_insert"}],
            "status": status,
            "message": "기존 Writer 저장 결과",
            "errors": [],
        },
        "trace": {"raw_text_preview": "원문 미리보기", "errors": []},
    }


def _authoring() -> dict:
    return {
        "request": {"raw_text": "장비 Assign 현황을 equipment_assign으로 등록해줘."},
        "refinement": {
            "refined_text": "equipment_assign 장비 Assign 현황 등록 요청입니다.",
            "missing_information": [],
            "assumptions": ["필수 파라미터 없음으로 해석했습니다."],
        },
        "items": [{"dataset_key": "equipment_assign"}],
        "existing_matches": [],
        "warnings": [{"type": "normalization", "message": "표준 컬럼을 정리했습니다."}],
        "errors": [],
        "review": {"errors": [], "warnings": []},
        "write_result": {"status": "dry_run", "errors": []},
        "trace": {
            "initial_transform": {
                "version": "table_catalog_initial_transform_v1",
                "status": "preserved_structured_contract",
            }
        },
    }


def test_portal_contract_fields_are_added_after_legacy_writer_without_changing_result() -> None:
    response = _response()
    original_response = deepcopy(response)

    enriched = ENRICHER.enrich_portal_contract(response, _authoring())

    # The compatibility layer is output-only: all existing save outcome fields
    # remain byte-for-byte equivalent, while Portal-only metadata is added.
    for key in ("status", "success", "message", "answer_sections", "data", "write_result"):
        assert enriched[key] == original_response[key]

    authoring = enriched["metadata_authoring"]
    assert authoring["contract_version"] == "metadata_authoring.rev_2.v1"
    assert authoring["original_text"] == "장비 Assign 현황을 equipment_assign으로 등록해줘."
    assert authoring["refined_text"] == "equipment_assign 장비 Assign 현황 등록 요청입니다."
    assert authoring["resolved_references"] == []
    assert authoring["missing_information"] == []
    assert authoring["assumptions"] == ["필수 파라미터 없음으로 해석했습니다."]
    assert authoring["contract_validation"] == {
        "status": "validated",
        "mode": "legacy_writer",
        "errors": [],
        "warnings": [{"type": "normalization", "message": "표준 컬럼을 정리했습니다."}],
        "note": "초기 변환 후 기존 Table Catalog Writer의 검토·저장 결과를 사용했습니다.",
    }
    assert enriched["trace"]["initial_transform"]["status"] == "preserved_structured_contract"
    assert enriched["trace"]["response_contract"] == {
        "version": "metadata_authoring.rev_2.v1",
        "mode": "legacy_table_catalog_writer_with_initial_transform_v1",
        "additive": True,
    }


def test_portal_contract_reports_legacy_writer_error_without_reclassifying_response() -> None:
    response = _response(status="error", message="기존 Writer 오류입니다.")
    response["success"] = False
    response["write_result"]["success"] = False
    response["write_result"]["errors"] = [
        {"type": "filter_mapping_source_column_missing", "message": "실제 컬럼이 없습니다."}
    ]
    original_response = deepcopy(response)
    authoring = _authoring()
    authoring["errors"] = [{"type": "normalizer_error", "message": "후보 정규화 오류"}]

    enriched = ENRICHER.enrich_portal_contract(response, authoring)

    for key in ("status", "success", "message", "answer_sections", "data", "write_result"):
        assert enriched[key] == original_response[key]
    validation = enriched["metadata_authoring"]["contract_validation"]
    assert validation["status"] == "error"
    assert {item["type"] for item in validation["errors"]} == {
        "normalizer_error",
        "filter_mapping_source_column_missing",
    }
    assert enriched["status"] == "error"
    assert enriched["status"] != "needs_input"


def test_duplicate_skip_is_not_displayed_as_a_contract_error() -> None:
    response = _response(status="skipped", message="기존 데이터셋을 유지하고 저장을 건너뛰었습니다.")
    response["success"] = False
    response["write_result"].update(
        {
            "success": False,
            "dry_run": False,
            "status": "skipped",
            "message": "duplicate_action=skip에 따라 저장을 건너뛰었습니다.",
        }
    )

    enriched = ENRICHER.enrich_portal_contract(response, _authoring())

    assert enriched["status"] == "skipped"
    assert enriched["message"] == "기존 데이터셋을 유지하고 저장을 건너뛰었습니다."
    assert enriched["metadata_authoring"]["contract_validation"]["status"] == "validated"
