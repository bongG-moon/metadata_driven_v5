from __future__ import annotations

from component_test_support import ROOT, load_module


REV2_ROOT = ROOT / "langflow_components" / "metadata_saving_rev_2_common"


def _response() -> dict:
    return {
        "metadata_type": "domain",
        "status": "needs_input",
        "success": False,
        "message": "보완 정보가 필요합니다.",
        "answer_sections": {
            "summary": {"headline": "보완 정보가 필요합니다."},
            "key_points": [],
            "notices": [],
            "next_steps": [],
        },
        "metadata_authoring": {},
        "trace": {},
        "data": {"columns": [], "rows": [], "row_count": 0},
    }


def test_response_surfaces_only_first_retry_recommendation_from_legacy_payload() -> None:
    """Legacy multi-option drafts must not expose multiple user-facing choices."""
    enricher = load_module(REV2_ROOT / "08_metadata_authoring_response_enricher_rev_2.py")
    adapter = load_module(REV2_ROOT / "09_metadata_authoring_message_adapter_rev_2.py")
    authoring_payload = {
        "request": {"raw_text": "테이블 메타데이터를 등록해줘."},
        "metadata_authoring_draft": {
            "retry_example": "",
            "retry_examples": ["권장안 1", "권장안 2", "권장안 3", "권장안 4"],
            "missing_information": ["dataset_key를 확인해 주세요."],
            "unresolved_references": [],
        },
        "refinement": {},
        "trace": {},
        "write_result": {},
    }

    enriched = enricher.enrich_response(_response(), authoring_payload)
    message = adapter.build_message(enriched)

    assert enriched["metadata_authoring"]["retry_example"] == "권장안 1"
    assert enriched["metadata_authoring"]["retry_examples"] == ["권장안 1"]
    assert enriched["answer_sections"]["next_steps"] == ["위 '다시 입력 예시'를 그대로 복사해 다시 실행하세요."]
    assert "권장안 1" in message
    assert all(item not in message for item in ("권장안 2", "권장안 3", "권장안 4", "#### 선택안"))


def test_failure_response_caps_next_steps_to_one_primary_action_without_hiding_errors() -> None:
    enricher = load_module(REV2_ROOT / "08_metadata_authoring_response_enricher_rev_2.py")
    response = _response()
    response["status"] = "error"
    response["answer_sections"]["next_steps"] = ["첫 번째 조치", "두 번째 조치"]
    authoring_payload = {
        "request": {"raw_text": "테이블 메타데이터를 등록해줘."},
        "metadata_authoring_draft": {
            "retry_example": "",
            "retry_examples": [],
            "missing_information": [],
            "unresolved_references": [],
        },
        "errors": [{"type": "invalid_contract", "message": "원본 오류 진단"}],
        "refinement": {},
        "trace": {},
        "write_result": {},
    }

    enriched = enricher.enrich_response(response, authoring_payload)

    assert enriched["status"] == "error"
    assert len(enriched["answer_sections"]["next_steps"]) == 1
    assert "메타데이터 계약 검증" in enriched["answer_sections"]["next_steps"][0]
    assert enriched["trace"]["write_status"] == ""


def test_table_catalog_response_surfaces_nonblocking_contract_normalization_as_info() -> None:
    response_builder = load_module(
        ROOT / "langflow_components" / "table_catalog_saving_flow" / "08_table_catalog_saving_response_builder.py"
    )
    response = response_builder.build_response(
        {
            "items": [],
            "warnings": [
                {
                    "type": "coalesced_equivalent_canonical_metric_semantics",
                    "message": "동일한 실행 컬럼의 물리/표준 metric 계약을 'EQP_ID' 하나로 정리했습니다.",
                }
            ],
            "write_result": {"success": True, "dry_run": True, "would_save_count": 1},
            "review": {"errors": [], "supplement_requests": [], "assumptions": []},
        }
    )

    assert response["status"] == "dry_run"
    assert {
        "type": "info",
        "title": "자동 정리",
        "message": "동일한 실행 컬럼의 물리/표준 metric 계약을 'EQP_ID' 하나로 정리했습니다.",
    } in response["answer_sections"]["notices"]
