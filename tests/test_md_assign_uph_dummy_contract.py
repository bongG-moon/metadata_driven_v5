from __future__ import annotations

import importlib.util
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_dummy_module():
    path = ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py"
    spec = importlib.util.spec_from_file_location("md_assign_uph_dummy_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_md_assign_and_uph_dummy_rows_cover_multiple_leads_with_matching_join_keys():
    """The M/D validation question must have real positive rows after the registered join."""

    dummy = _load_dummy_module()
    join_keys = ("EQP_MODEL", "RECIPE_ID", "OPER_NAME", "LEAD")
    assign_rows = [
        row
        for row in dummy._rows_for_dataset("equipment_assign")
        if row.get("OPER_NAME") == "M/D"
    ]
    uph_by_key = {
        tuple(row.get(key) for key in join_keys): row["UPH"]
        for row in dummy._rows_for_dataset("eqp_uph")
        if row.get("OPER_NAME") == "M/D"
    }

    grouped = defaultdict(lambda: {"assign_ids": set(), "uph_values": []})
    for row in assign_rows:
        key = tuple(row.get(column) for column in join_keys)
        lead = str(row["LEAD"])
        grouped[lead]["assign_ids"].add(row["EQP_ID"])
        grouped[lead]["uph_values"].append(uph_by_key[key])

    assert {
        lead: {
            "assign_count": len(value["assign_ids"]),
            "avg_uph": sum(value["uph_values"]) / len(value["uph_values"]),
        }
        for lead, value in grouped.items()
    } == {
        "78": {"assign_count": 2, "avg_uph": 140.0},
        "267": {"assign_count": 1, "avg_uph": 119.0},
        "266": {"assign_count": 2, "avg_uph": 130.0},
    }
