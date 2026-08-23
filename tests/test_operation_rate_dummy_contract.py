from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_dummy_module():
    path = ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py"
    spec = importlib.util.spec_from_file_location("operation_rate_dummy_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operation_rate_today_joins_da_l217_assign_equipment_without_integrated_attributes():
    dummy = _load_dummy_module()
    assign_rows = dummy._rows_for_dataset("equipment_assign")
    rate_rows = [
        row
        for row in dummy._rows_for_dataset("operation_rate_today")
        if row.get("WORK_DT") == "20260701"
    ]

    expected_columns = {
        "WORK_DT",
        "EQP_MODEL_CD",
        "EQP_ID",
        "EQP_EFFIC_CD",
        "TOTAL_INTERVAL_TM",
        "FLOW_TM",
        "TOTAL_INTERVAL_RATE",
    }
    assert rate_rows
    assert all(set(row) == expected_columns for row in rate_rows)

    assign_by_equipment = {row["EQP_ID"]: row for row in assign_rows}
    rate_by_equipment = {row["EQP_ID"]: row for row in rate_rows}
    assert {
        equipment_id: (
            assign_by_equipment[equipment_id]["OPER_NAME"],
            assign_by_equipment[equipment_id]["MCP_NO"],
            rate_by_equipment[equipment_id]["TOTAL_INTERVAL_RATE"],
        )
        for equipment_id in ("EQP-DA217-A", "EQP-DA217-B")
    } == {
        "EQP-DA217-A": ("D/A1", "L-217K9B", 86.5),
        "EQP-DA217-B": ("D/A2", "L-217K9B", 72.0),
    }


def test_operation_rate_today_preserves_existing_equipment_rows():
    dummy = _load_dummy_module()
    equipment_ids = {
        row["EQP_ID"]
        for row in dummy._rows_for_dataset("operation_rate_today")
        if row.get("WORK_DT") == "20260701"
    }
    assert {"EQP002", "EQP003", "EQP004", "D724"} <= equipment_ids
