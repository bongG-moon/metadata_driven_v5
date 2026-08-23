from __future__ import annotations

from component_test_support import ROOT, load_module


ADAPTER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "21_v2_answer_message_adapter.py"
)


def _adapter():
    return load_module(ADAPTER_PATH)


def test_empty_result_table_hides_column_list_for_answer_sections_path():
    adapter = _adapter()
    payload = {
        "answer_sections": {
            "summary": {"headline": "조건에 맞는 데이터가 없습니다."},
            "result_table": {
                "columns": ["LEAD", "EQUIPMENT_ASSIGN_COUNT", "AVG_UPH"],
                "rows": [],
                "row_count": 0,
            },
        },
        "data": {
            "columns": ["LEAD", "EQUIPMENT_ASSIGN_COUNT", "AVG_UPH"],
            "rows": [],
            "row_count": 0,
        },
    }

    message = adapter.build_message(
        payload,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "### 결과 테이블\n표시할 결과 행이 없습니다." in message
    assert "컬럼:" not in message
    assert "EQUIPMENT_ASSIGN_COUNT" not in message


def test_empty_result_table_hides_column_list_for_legacy_payload_path():
    adapter = _adapter()
    payload = {
        "answer_message": "조건에 맞는 데이터가 없습니다.",
        "data": {
            "columns": ["LEAD", "EQUIPMENT_ASSIGN_COUNT", "AVG_UPH"],
            "rows": [],
            "row_count": 0,
        },
    }

    message = adapter.build_message(
        payload,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "### 결과 테이블\n표시할 결과 행이 없습니다." in message
    assert "컬럼:" not in message
    assert "EQUIPMENT_ASSIGN_COUNT" not in message


def test_empty_intermediate_result_hides_column_list():
    adapter = _adapter()
    payload = {
        "answer_message": "조건에 맞는 데이터가 없습니다.",
        "data": {"columns": [], "rows": [], "row_count": 0},
        "intermediate_results": [
            {
                "description": "필터 적용 후 중간 데이터",
                "role": "source_filtered",
                "columns": ["LEAD", "EQUIPMENT_ASSIGN_COUNT", "AVG_UPH"],
                "preview_rows": [],
                "row_count": 0,
            }
        ],
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_intermediate_results=True,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "#### 필터 적용 후 중간 데이터\n표시할 행이 없습니다." in message
    assert "컬럼:" not in message
    assert "EQUIPMENT_ASSIGN_COUNT" not in message
