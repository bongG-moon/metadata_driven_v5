from __future__ import annotations

from component_test_support import ROOT, load_module


CLEANUP_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "24_runtime_payload_cleanup.py"


def _module():
    return load_module(CLEANUP_PATH)


def _rows(prefix: str, count: int, *, include_extra_columns: bool = False) -> list[dict]:
    rows: list[dict] = []
    for index in range(1, count + 1):
        row = {
            "DATE": "20260823",
            "OPER_NAME": "D/A1",
            "LEAD": 78,
            "EQP_ID": f"{prefix}-{index:02d}",
        }
        if include_extra_columns:
            row.update({f"EXTRA_{column:02d}": f"{prefix}-{index}-{column}" for column in range(1, 23)})
        rows.append(row)
    return rows


def test_cleanup_builds_bounded_html_preview_with_existing_csv_refs_and_releases_buffers():
    cleanup = _module()
    source_rows = _rows("assign", 12, include_extra_columns=True)
    intermediate_rows = _rows("filtered", 12)
    final_rows = [
        {
            "LEAD": 78,
            "equipment_count": index,
            "avg_uph": 100 + index,
            "holding_capacity": (100 + index) * index * 24,
        }
        for index in range(1, 36)
    ]
    expected_display_rows = [dict(row) for row in final_rows[:3]]
    source_columns = ["DATE", "OPER_NAME", "LEAD", "EQP_ID"] + [f"EXTRA_{column:02d}" for column in range(1, 23)]
    payload = {
        "source_results": [
            {
                "dataset_key": "equipment_assign",
                "source_alias": "assign",
                "row_count": 12,
                "columns": source_columns,
            }
        ],
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "assign",
                    "required_params": {"DATE": "20260823"},
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "D/A1"},
                        "LEAD": {"operator": "eq", "value": 78},
                    },
                }
            ]
        },
        "runtime_sources": {"ASSIGN": source_rows},
        "_runtime_rows_by_alias": {"ASSIGN": list(source_rows)},
        "_intermediate_download_rows": {
            "filtered_assign": {
                "rows": intermediate_rows,
                "columns": ["DATE", "OPER_NAME", "LEAD", "EQP_ID"],
                "row_count": 12,
                "label": "장비 Assign 필터 적용 후",
                "checkpoint_key": "filtered:assign",
            }
        },
        "_intermediate_download_metadata": {
            "filtered_assign": {
                "label": "장비 Assign 필터 적용 후",
                "checkpoint_key": "filtered:assign",
            }
        },
        "_full_result_rows": final_rows,
        "_runtime_result_rows": [{"stale": True}],
        "data": {
            "columns": ["LEAD", "equipment_count", "avg_uph", "holding_capacity"],
            "row_count": 35,
            "rows": expected_display_rows,
        },
        "data_refs": [
            {
                "role": "source_rows",
                "source_alias": "assign",
                "download_url": "https://api.example/download/source-assign.csv",
                "expires_at": "2026-08-23T03:00:00+00:00",
                "complete": True,
            },
            {
                "role": "intermediate_result",
                "checkpoint_key": "filtered:assign",
                "download_url": "https://api.example/download/filtered-assign.csv",
                "expires_at": "2026-08-23T03:00:00+00:00",
                "complete": True,
            },
            {
                "role": "analysis_result",
                "download_url": "https://api.example/download/final-result.csv",
                "expires_at": "2026-08-23T03:00:00+00:00",
                "complete": True,
            },
        ],
        "_execution_report_domain_details": [
            {
                "section": "analysis_recipes",
                "key": "HELD_CAPA_CALCULATION",
                "title": "보유CAPA 계산",
                "summary": "장비 보유 대수와 평균 UPH로 보유 CAPA를 계산합니다.",
                "details": {
                    "formula": "equipment_count × avg_uph × 24",
                    "source_config": "must-not-reach-answer",
                },
            }
        ],
    }

    cleaned = cleanup.release_runtime_payload(payload, gc_mode="disabled")

    preview = cleaned[cleanup.EXECUTION_REPORT_DATA_PREVIEW_KEY]
    assert set(preview) == {"original", "intermediate", "final", "domains"}

    original = preview["original"][0]
    assert original["title"] == "사용 원본 데이터: equipment_assign"
    assert original["row_count"] == 12
    assert original["shown_row_count"] == cleanup.MAX_REPORT_SOURCE_ROWS
    assert original["truncated"] is True
    assert original["columns"][:3] == ["DATE", "OPER_NAME", "LEAD"]
    assert len(original["columns"]) == cleanup.MAX_REPORT_COLUMNS
    assert original["columns_truncated"] is True
    assert original["rows"][0]["EQP_ID"] == "assign-01"
    assert original["download"]["url"] == "https://api.example/download/source-assign.csv"

    intermediate = preview["intermediate"][0]
    assert intermediate["title"] == "장비 Assign 필터 적용 후"
    assert intermediate["row_count"] == 12
    assert intermediate["shown_row_count"] == cleanup.MAX_REPORT_INTERMEDIATE_ROWS
    assert intermediate["truncated"] is True
    assert intermediate["download"]["url"] == "https://api.example/download/filtered-assign.csv"

    final = preview["final"][0]
    assert final["title"] == "최종 결과 데이터"
    assert final["row_count"] == 35
    assert final["shown_row_count"] == cleanup.MAX_REPORT_FINAL_ROWS
    assert final["truncated"] is True
    assert final["rows"][0]["holding_capacity"] == 2424
    assert final["download"]["url"] == "https://api.example/download/final-result.csv"

    assert preview["domains"][0]["key"] == "HELD_CAPA_CALCULATION"
    assert "_execution_report_domain_details" not in cleaned

    assert cleaned["data"]["rows"] == expected_display_rows
    for key in cleanup.RUNTIME_BUFFER_KEYS:
        assert key not in cleaned
    assert payload["runtime_sources"] == {}
    assert payload["_runtime_rows_by_alias"] == {}
    assert payload["_intermediate_download_rows"] == {}
    assert payload["_intermediate_download_metadata"] == {}
    assert payload["_full_result_rows"] == []
    assert payload["_runtime_result_rows"] == []
    assert payload["_execution_report_domain_details"] == []
    assert cleaned["runtime_cleanup"]["released_row_count"] > 0
    assert cleaned["runtime_cleanup"]["released_buffer_count"] > 0
