from __future__ import annotations

from component_test_support import ROOT, load_module


TRANSFORMER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "01_table_catalog_initial_transformer.py"
)


def _payload(raw_text: str) -> dict:
    return {
        "metadata_type": "table_catalog",
        "request": {"raw_text": raw_text, "dry_run": True, "duplicate_action": "skip"},
        "refinement": {
            "refined_text": "",
            "needs_more_input": True,
            "missing_information": ["old"],
            "assumptions": ["keep this assumption"],
        },
        "errors": [],
        "warnings": [],
        "trace": {},
    }


STRUCTURED_ASSIGN_REQUEST = """
장비 Assign 현황 데이터는 equipment_assign으로 등록해줘.

query_template:
SELECT EQUIP_ID, DENSITY, PKG1, OPER
FROM EQP_TABLE
WHERE 1=1

filter_mappings는 EQP_ID -> EQUIP_ID, DEN -> DENSITY,
PKG_TYPE1 -> PKG1, OPER_NUM -> OPER 로 연결해줘.
""".strip()


def test_structured_contract_keeps_raw_request_when_llm_changes_mapping_direction() -> None:
    result = TRANSFORMER.transform_initial_text(
        _payload(STRUCTURED_ASSIGN_REQUEST),
        {
            "refined_text": """
            query_template:\nSELECT EQP_ID, DEN FROM EQP_TABLE
            filter_mappings는 DEN -> DEN, PKG_TYPE1 -> PKG_TYPE1 로 연결해줘.
            """,
        },
    )

    assert result["refinement"]["refined_text"] == STRUCTURED_ASSIGN_REQUEST
    assert result["refinement"]["needs_more_input"] is False
    assert result["refinement"]["missing_information"] == []
    assert result["refinement"]["assumptions"] == ["keep this assumption"]
    assert result["errors"] == []
    assert result["trace"]["initial_transform"] == {
        "version": "table_catalog_initial_transform_v1",
        "status": "preserved_structured_contract",
        "reason": "query_or_mapping_contract_present",
        "input_shape": "structured_contract",
        "llm_response_format": "json_object",
        "raw_text_length": len(STRUCTURED_ASSIGN_REQUEST),
        "refined_text_length": len(STRUCTURED_ASSIGN_REQUEST),
    }


def test_plain_request_accepts_json_or_plain_text_without_creating_a_gate() -> None:
    raw = "오늘 생산 실적 데이터를 production으로 등록해줘. 표시명은 Production History야."
    json_result = TRANSFORMER.transform_initial_text(
        _payload(raw),
        '```json\n{"refined_text":"production 이력 생산 실적 데이터 등록 요청입니다."}\n```',
    )
    assert json_result["refinement"]["refined_text"] == "production 이력 생산 실적 데이터 등록 요청입니다."
    assert json_result["refinement"]["needs_more_input"] is False
    assert json_result["refinement"]["missing_information"] == []
    assert json_result["errors"] == []
    assert json_result["trace"]["initial_transform"]["status"] == "applied"
    assert json_result["trace"]["initial_transform"]["llm_response_format"] == "json_fenced"

    plain_result = TRANSFORMER.transform_initial_text(_payload(raw), "production 이력 생산 실적 데이터 등록 요청입니다.")
    assert plain_result["refinement"]["refined_text"] == "production 이력 생산 실적 데이터 등록 요청입니다."
    assert plain_result["trace"]["initial_transform"]["status"] == "applied"
    assert plain_result["trace"]["initial_transform"]["llm_response_format"] == "plain_text"


def test_invalid_transform_response_falls_back_to_raw_without_adding_errors() -> None:
    raw = "이력 생산 실적 데이터를 production으로 등록해줘."
    result = TRANSFORMER.transform_initial_text(_payload(raw), '{"other": "value"}')

    assert result["refinement"]["refined_text"] == raw
    assert result["refinement"]["needs_more_input"] is False
    assert result["refinement"]["missing_information"] == []
    assert result["errors"] == []
    assert result["trace"]["initial_transform"]["status"] == "fallback_raw"


def test_source_text_output_always_uses_the_original_request() -> None:
    payload = _payload(STRUCTURED_ASSIGN_REQUEST)
    assert TRANSFORMER.source_text_from_payload(payload) == STRUCTURED_ASSIGN_REQUEST
