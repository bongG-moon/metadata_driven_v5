"""Deterministic dummy rows that mirror the local data_catalog.txt shape."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PRODUCTS = [
    {
        "FAMILY": "MEMORY",
        "MODE": "LPDDR5",
        "DENSITY": "16G",
        "TECH": "1Z",
        "ORG": "PKG",
        "PKG1": "LFBGA",
        "PKG2": "POP",
        "LEAD": "200",
        "MCP_NO": "M-001",
        "TSV_DIE_TYP": "8Hi",
        "DEVICE": "DEV001",
        "DEVICE_DESC": "LPDDR5 sample",
    },
    {
        "FAMILY": "HBM",
        "MODE": "HBM3E",
        "DENSITY": "24G",
        "TECH": "1A",
        "ORG": "PKG",
        "PKG1": "HBM",
        "PKG2": "TSV",
        "LEAD": "300",
        "MCP_NO": "H-001",
        "TSV_DIE_TYP": "12Hi",
        "DEVICE": "DEV-HBM",
        "DEVICE_DESC": "HBM3E sample",
    },
    {
        "FAMILY": "MOBILE",
        "MODE": "LPDDR5X",
        "DENSITY": "32G",
        "TECH": "1B",
        "ORG": "PKG",
        "PKG1": "UFBGA",
        "PKG2": "MOBILE",
        "LEAD": "180",
        "MCP_NO": "M-002",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV002",
        "DEVICE_DESC": "LPDDR5X sample",
    },
]

PROCESSES = [
    {"OPER": "DA1", "OPER_NAME": "D/A1", "OPER_SEQ": "100"},
    {"OPER": "DA2", "OPER_NAME": "D/A2", "OPER_SEQ": "110"},
    {"OPER": "WB1", "OPER_NAME": "W/B1", "OPER_SEQ": "200"},
    {"OPER": "WB2", "OPER_NAME": "W/B2", "OPER_SEQ": "210"},
]


def rows_for_dataset(dataset_key: str) -> list[dict[str, Any]]:
    key = str(dataset_key or "").lower()
    rows = {
        "production_today": _production_rows("20260701", [1000, 700, 650]),
        "production": _production_rows("20260630", [900, 620, 580]),
        "wip_today": _wip_rows("20260701", [120, 80, 75, 55]),
        "wip": _wip_rows("20260630", [130, 85, 65, 45]),
        "target": _target_rows(),
        "equipment_assign": _equipment_assign_rows(),
        "equipment_status": _equipment_assign_rows(),
        "eqp_uph": _eqp_uph_rows(),
        "lot_status": _lot_status_rows(),
        "hold_history": _hold_history_rows(),
    }.get(key, [])
    return deepcopy(rows)


def _production_rows(work_date: str, quantities: list[int]) -> list[dict[str, Any]]:
    rows = []
    for index, quantity in enumerate(quantities):
        rows.append(_product_process_row(work_date, index, index, PRODUCTION=quantity))
    return rows


def _wip_rows(work_date: str, quantities: list[int]) -> list[dict[str, Any]]:
    rows = []
    for index, quantity in enumerate(quantities):
        rows.append(_product_process_row(work_date, index, index, WIP=quantity, LOT_ID=f"L{index + 1:03d}"))
    return rows


def _target_rows() -> list[dict[str, Any]]:
    rows = []
    for index, product in enumerate(PRODUCTS):
        rows.append(
            {
                "DATE": "2026-07-01",
                "Mode": product["MODE"],
                "DEN": product["DENSITY"],
                "TECH": product["TECH"],
                "PKG1": product["PKG1"],
                "PKG2": product["PKG2"],
                "LEAD": product["LEAD"],
                "ORG": product["ORG"],
                "MCP NO": product["MCP_NO"],
                "INPUT 계획": 800 - index * 100,
                "OUT 계획": 1200 - index * 150,
                "MODE": product["MODE"],
                "DENSITY": product["DENSITY"],
                "PKG_TYPE1": product["PKG1"],
                "PKG_TYPE2": product["PKG2"],
                "MCP_NO": product["MCP_NO"],
                "INPUT_PLAN": 800 - index * 100,
                "OUT_PLAN": 1200 - index * 150,
                "TARGET": 1200 - index * 150,
            }
        )
    return rows


def _equipment_assign_rows() -> list[dict[str, Any]]:
    rows = []
    for index, product in enumerate(PRODUCTS):
        process = PROCESSES[index % len(PROCESSES)]
        model = ["EQM-A", "EQM-HBM", "EQM-MOBILE"][index]
        rows.append(
            {
                "BAY_ID": f"BAY{index + 1:02d}",
                "EQUIP_ID": f"EQP{index + 1:03d}",
                "EQUIP_MODEL": model,
                "PRESS_CNT": [2, 4, 1][index],
                "OPER": process["OPER"],
                "OPER_NM": process["OPER_NAME"],
                "MODE": product["MODE"],
                "DENSITY": product["DENSITY"],
                "TECH": product["TECH"],
                "PKG1": product["PKG1"],
                "PKG2": product["PKG2"],
                "LEAD": product["LEAD"],
                "ORG": product["ORG"],
                "PKGSIZE": ["12x12", "18x18", "10x10"][index],
                "MCP_NO": product["MCP_NO"],
                "DEVICE": product["DEVICE"],
                "DEVICE_DESC": product["DEVICE_DESC"],
                "LOT_ID": "T1234567GEN1" if index == 0 else f"T765432{index}GEN1",
                "RECIPE_ID": f"RCP-{index + 1:03d}",
                "EQP_ID": f"EQP{index + 1:03d}",
                "EQP_MODEL": model,
                "EQPIP_MODEL": model,
                "DEN": product["DENSITY"],
                "PKG_TYPE1": product["PKG1"],
                "PKG_TYPE2": product["PKG2"],
                "OPER_NAME": process["OPER_NAME"],
                "OPER_NUM": process["OPER"],
            }
        )
    return rows


def _eqp_uph_rows() -> list[dict[str, Any]]:
    rows = []
    for index, product in enumerate(PRODUCTS):
        process = PROCESSES[index % len(PROCESSES)]
        model = ["EQM-A", "EQM-HBM", "EQM-MOBILE"][index]
        rows.append(
            {
                "EQUIP_MODEL": model,
                "OPER": process["OPER"],
                "OPER_NAME": process["OPER_NAME"],
                "PRESS_CNT": [2, 4, 1][index],
                "MODE": product["MODE"],
                "TECH": product["TECH"],
                "ORG": product["ORG"],
                "DENSITY": product["DENSITY"],
                "PKG1": product["PKG1"],
                "PKG2": product["PKG2"],
                "LEAD": product["LEAD"],
                "MCP_NO": product["MCP_NO"],
                "RECIPE_ID": f"RCP-{index + 1:03d}",
                "UPH": [123.4, 88.2, 156.7][index],
                "LOAD_DT": "20260701",
                "BASE_DT": "20260701",
                "EQP_MODEL": model,
                "DEN": product["DENSITY"],
                "PKG_TYPE1": product["PKG1"],
                "PKG_TYPE2": product["PKG2"],
                "OPER_NUM": process["OPER"],
            }
        )
    return rows


def _lot_status_rows() -> list[dict[str, Any]]:
    base = _product_process_row("20260701", 0, 0)
    rows = [
        _lot_row(base, "T1234567GEN1", "OnHold", "WAITING", 100, 25, 12.5, 40.0, "검증용 HOLD"),
        _lot_row(_product_process_row("20260701", 1, 1), "T7654321GEN1", "NotOnHold", "RUNNING", 80, 20, 5.0, 25.0, ""),
        _lot_row(_product_process_row("20260701", 2, 2), "T2222222GEN1", "NotOnHold", "WAITING", 60, 18, 3.5, 18.0, ""),
    ]
    return rows


def _hold_history_rows() -> list[dict[str, Any]]:
    first = _product_process_row("20260701", 0, 0)
    second = _product_process_row("20260701", 1, 1)
    return [
        _hold_row(first, "T1234567GEN1", "H001", "검증용 HOLD 이력"),
        _hold_row(second, "T7654321GEN1", "H002", "레시피 확인 HOLD"),
    ]


def _product_process_row(work_date: str, product_index: int, process_index: int, **overrides: Any) -> dict[str, Any]:
    product = PRODUCTS[product_index % len(PRODUCTS)]
    process = PROCESSES[process_index % len(PROCESSES)]
    row = {
        "WORK_DATE": work_date,
        "WORK_DT": work_date,
        "SHIFT": str((process_index % 3) + 1),
        "FACTORY": "PNT",
        "FAB": "PKG",
        "FAMILY": product["FAMILY"],
        "MODE": product["MODE"],
        "DENSITY": product["DENSITY"],
        "DEN": product["DENSITY"],
        "TECH": product["TECH"],
        "ORG": product["ORG"],
        "PKG1": product["PKG1"],
        "PKG_TYPE1": product["PKG1"],
        "PKG2": product["PKG2"],
        "PKG_TYPE2": product["PKG2"],
        "LEAD": product["LEAD"],
        "MCP_NO": product["MCP_NO"],
        "TSV_DIE_TYP": product["TSV_DIE_TYP"],
        "TSV_DIE_TYPE": product["TSV_DIE_TYP"],
        "DEVICE": product["DEVICE"],
        "DEVICE_DESC": product["DEVICE_DESC"],
        "DIE_ATTACH_QTY": product_index + 1,
        "NETDIE_300_CNT": 100 + product_index * 20,
        "OPER": process["OPER"],
        "OPER_NUM": process["OPER"],
        "OPER_NAME": process["OPER_NAME"],
        "OPER_NM": process["OPER_NAME"],
        "OPER_SEQ": process["OPER_SEQ"],
    }
    row.update(overrides)
    return row


def _lot_row(
    base: dict[str, Any],
    lot_id: str,
    hold_stat: str,
    lot_stat: str,
    prod_qty: int,
    wf_qty: int,
    in_tat: float,
    cum_tat: float,
    hold_reason: str,
) -> dict[str, Any]:
    return {
        "ERM_ID": "ERM-PKG",
        "OPER": base["OPER"],
        "OPER_NAME": base["OPER_NAME"],
        "FAB": base["FAB"],
        "OWNER": "PNT",
        "GRADE": "A",
        "DEVICE": base["DEVICE"],
        "LOT_ID": lot_id,
        "SUB_LOT_ID": f"{lot_id}-S1",
        "PROD_QTY": prod_qty,
        "WF_QTY": wf_qty,
        "IN_TAT": in_tat,
        "CUM_TAT": cum_tat,
        "EQP_ID": "EQP001",
        "FLOW_ID": "FLOW-PKG",
        "OPER_IN_TM": "2026-07-01 07:10:00",
        "FAC_IN_TIME": "2026-07-01 04:00:00",
        "HOLD_STAT": hold_stat,
        "HOLD_REASON": hold_reason,
        "FAMILY": base["FAMILY"],
        "MODE": base["MODE"],
        "DENSITY": base["DENSITY"],
        "TECH": base["TECH"],
        "ORG": base["ORG"],
        "PKG1": base["PKG1"],
        "PKG2": base["PKG2"],
        "PKG3": "",
        "LEAD": base["LEAD"],
        "MCP_NO": base["MCP_NO"],
        "THK_CD": "STD",
        "LOT_STAT": lot_stat,
        "LOT_GRP": "NORMAL",
        "PKG_SIZE": "12x12",
        "HOT_LOT": "N",
        "HOT_LEVEL": "",
        "PKG_COMPOSIT": "",
        "DURABLE_ID": "",
        "DURABLE_TYP": "",
        "SUB_QTY": prod_qty,
        "TSV_DIE_TYPE": base["TSV_DIE_TYPE"],
        "EVENT_DESC": "HOLD" if hold_stat == "OnHold" else "MOVE",
        "MOVE_IN_TM": "2026-07-01 07:10:00",
        "PAD_ABNORMAL": "N",
        "SWR_REQ_NO": "",
        "INSP_TARGET": "N",
        "DEN": base["DENSITY"],
        "PKG_TYPE1": base["PKG1"],
        "PKG_TYPE2": base["PKG2"],
        "TSV_DIE_TYP": base["TSV_DIE_TYPE"],
        "OPER_NUM": base["OPER"],
        "DEVICE_DESC": base["DEVICE_DESC"],
    }


def _hold_row(base: dict[str, Any], lot_id: str, hold_cd: str, hold_desc: str) -> dict[str, Any]:
    return {
        "LOT_ID": lot_id,
        "PROD_QTY": 100,
        "OPER": base["OPER"],
        "OPER_NAME": base["OPER_NAME"],
        "HOLD_TM": "2026-07-01 08:00:00",
        "HOLD_CD": hold_cd,
        "HOLD_USER": "USER01",
        "HOLD_DESC": hold_desc,
        "FAB": base["FAB"],
        "FAMILY": base["FAMILY"],
        "MODE": base["MODE"],
        "DENSITY": base["DENSITY"],
        "TECH": base["TECH"],
        "ORG": base["ORG"],
        "PKG1": base["PKG1"],
        "PKG2": base["PKG2"],
        "LEAD": base["LEAD"],
        "MCP_NO": base["MCP_NO"],
        "GRADE": "A",
        "OWNER": "PNT",
        "DEVICE": base["DEVICE"],
        "DEVICE_DESC": base["DEVICE_DESC"],
        "PKG_SIZE": "12x12",
        "THK_CD": "STD",
        "flow_id": "FLOW-PKG",
        "DEN": base["DENSITY"],
        "PKG_TYPE1": base["PKG1"],
        "PKG_TYPE2": base["PKG2"],
        "TSV_DIE_TYPE": base["TSV_DIE_TYPE"],
        "TSV_DIE_TYP": base["TSV_DIE_TYPE"],
        "OPER_NUM": base["OPER"],
    }
