# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08 더미 데이터 조회기
# 역할: 실제 소스 조회가 꺼져 있을 때 데이터 조회 작업을 data_catalog 구조와 같은 더미 행으로 실행합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 조회 페이로드 (retrieval_payload)
# 처리 흐름: 실데이터 없이도 대표 질문을 검증할 수 있도록 데이터셋별 fixture에 날짜·제품·공정 조건을 동일한 규칙으로 적용합니다.
# 유지보수 포인트: 실행 오류를 다른 source의 성공처럼 위장하는 과도한 fallback은 만들지 말고 공통 errors 계약으로 전달합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

KOREA_ZONE_NAME = "Asia/Seoul"
PREVIEW_LIMIT = 5


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
        "TSV_DIE_TYP": "",
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
        "MCP_NO": "",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV002",
        "DEVICE_DESC": "LPDDR5X sample",
    },
    {
        "FAMILY": "MOBILE",
        "MODE": "LPDDR5",
        "DENSITY": "16G",
        "TECH": "1C",
        "ORG": "PKG",
        "PKG1": "LFBGA",
        "PKG2": "MOBILE",
        "LEAD": "267",
        "MCP_NO": "L-267A1",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV-L267",
        "DEVICE_DESC": "L-267 input mobile sample",
    },
    {
        "FAMILY": "GRAPHICS",
        "MODE": "GDDR6",
        "DENSITY": "16G",
        "TECH": "DA",
        "ORG": "PKG",
        "PKG1": "FBGA",
        "PKG2": "GRAPHICS",
        "LEAD": "180",
        "MCP_NO": "",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV-DA-GDDR6",
        "DEVICE_DESC": "DA 16G GDDR6 180 product",
    },
    {
        "FAMILY": "DDR",
        "MODE": "DDR4",
        "DENSITY": "32G",
        "TECH": "RG",
        "ORG": "DDP",
        "PKG1": "FBGA",
        "PKG2": "DDP",
        "LEAD": "96",
        "MCP_NO": "",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV-RG-DDR4",
        "DEVICE_DESC": "RG 32G DDR4 FBGA 96 DDP product",
    },
    {
        "FAMILY": "DDR",
        "MODE": "DDR5",
        "DENSITY": "16G",
        "TECH": "SP",
        "ORG": "4",
        "PKG1": "FCBGA",
        "PKG2": "SDP",
        "LEAD": "78",
        "MCP_NO": "",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV-SP-DDR5",
        "DEVICE_DESC": "SP 16G DDR5 2ND X4 78 FCBGA SDP product",
    },
    {
        "FAMILY": "MCP",
        "MODE": "LPDDR4",
        "DENSITY": "8G",
        "TECH": "1Y",
        "ORG": "PKG",
        "PKG1": "FBGA",
        "PKG2": "MCP",
        "LEAD": "218",
        "MCP_NO": "L-218K8H",
        "TSV_DIE_TYP": "",
        "DEVICE": "DEV-L218K8H",
        "DEVICE_DESC": "L-218K8H product",
    },
]

PROCESSES = [
    {"OPER": "INPUT", "OPER_NAME": "INPUT", "OPER_SEQ": "010"},
    {"OPER": "DA1", "OPER_NAME": "D/A1", "OPER_SEQ": "100"},
    {"OPER": "DA2", "OPER_NAME": "D/A2", "OPER_SEQ": "110"},
    {"OPER": "DA3", "OPER_NAME": "D/A3", "OPER_SEQ": "120"},
    {"OPER": "DA4", "OPER_NAME": "D/A4", "OPER_SEQ": "130"},
    {"OPER": "DA5", "OPER_NAME": "D/A5", "OPER_SEQ": "140"},
    {"OPER": "DA6", "OPER_NAME": "D/A6", "OPER_SEQ": "150"},
    {"OPER": "DS1", "OPER_NAME": "D/S1", "OPER_SEQ": "160"},
    {"OPER": "WB1", "OPER_NAME": "W/B1", "OPER_SEQ": "200"},
    {"OPER": "WB2", "OPER_NAME": "W/B2", "OPER_SEQ": "210"},
    {"OPER": "WB3", "OPER_NAME": "W/B3", "OPER_SEQ": "220"},
    {"OPER": "WB4", "OPER_NAME": "W/B4", "OPER_SEQ": "230"},
    {"OPER": "WB5", "OPER_NAME": "W/B5", "OPER_SEQ": "240"},
    {"OPER": "WB6", "OPER_NAME": "W/B6", "OPER_SEQ": "250"},
    {"OPER": "FCB1", "OPER_NAME": "FCB1", "OPER_SEQ": "300"},
    {"OPER": "FCB2", "OPER_NAME": "FCB2", "OPER_SEQ": "310"},
    {"OPER": "FCBH", "OPER_NAME": "FCB/H", "OPER_SEQ": "320"},
    {"OPER": "BG1", "OPER_NAME": "B/G1", "OPER_SEQ": "400"},
    {"OPER": "BG2", "OPER_NAME": "B/G2", "OPER_SEQ": "410"},
    {"OPER": "SBM", "OPER_NAME": "SBM", "OPER_SEQ": "500"},
    {"OPER": "PKGOUT", "OPER_NAME": "PKG OUT", "OPER_SEQ": "900"},
]


# 주요 함수: 테스트 fixture를 실제 조회 결과와 같은 source result 계약으로 반환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def retrieve_dummy_data(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if not jobs:
        return _skipped("dummy", "no dummy retrieval jobs")
    results = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        rows = _rows_for_dataset(str(job.get("dataset_key") or ""))
        rows = _apply_params(rows, job.get("required_params"))
        results.append(
            {
                "source_alias": job.get("source_alias") or job.get("dataset_key"),
                "dataset_key": job.get("dataset_key"),
                "source_type": job.get("source_type"),
                "status": "ok",
                "row_count": len(rows),
                "columns": sorted({column for row in rows for column in row}),
                "preview_rows": rows[:PREVIEW_LIMIT],
                "rows": rows,
                "applied_params": job.get("required_params", {}),
                "pandas_filters": job.get("filters", {}),
                "data_ref": "",
                "source_execution": {
                    "used_dummy_data": True,
                    "adapter": "dummy",
                    "declared_source_type": job.get("source_type"),
                    "params_applied_in_retriever": True,
                    "filters_applied_in_retriever": False,
                },
                "warnings": [],
                "errors": [],
            }
        )
    return {"source_type": "dummy", "status": "ok", "skipped": False, "source_results": results, "errors": [], "warnings": []}


# 함수 설명: `_skipped()`는 설정이나 대상 작업이 없어 실행하지 않은 이유를 표준 skipped 결과로 남깁니다.
def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


# 함수 설명: `_rows_for_dataset()`는 dataset_key에 해당하는 dummy fixture 행을 복사해 조회 조건 적용 전 원본으로 제공합니다.
def _rows_for_dataset(dataset_key: str) -> list[dict[str, Any]]:
    today = _korea_today()
    yesterday = _date_delta(today, -1)
    two_days_ago = _date_delta(today, -2)
    rows = {
        "production_today": _production_rows(_unique_dates([today, "20260701"])),
        "production": _production_rows(_unique_dates([yesterday, "20260701", "20260630", "20260627", "20260624"])),
        "wip_today": _wip_rows(_unique_dates([today, "20260701"])),
        "wip": _wip_rows(_unique_dates([yesterday, two_days_ago, "20260701", "20260630", "20260626", "20260624", "20260623"])),
        "product_token_fixture": _product_token_fixture_rows(_unique_dates([today, "20260701"])),
        "target": _target_rows(),
        "equipment_assign": _equipment_assign_rows(),
        "equipment_status": _equipment_assign_rows(),
        "eqp_uph": _eqp_uph_rows(),
        "lot_status": _lot_status_rows(),
        "hold_history": _hold_history_rows(),
    }.get(dataset_key, [])
    return deepcopy(rows)


# 함수 설명: `_korea_today()`는 현재 시각을 한국 시간 기준 YYYYMMDD 날짜 문자열로 반환합니다.
def _korea_today() -> str:
    return datetime.now(_korea_timezone()).strftime("%Y%m%d")


# 함수 설명: `_korea_timezone()`는 표준 zoneinfo를 우선 사용하고 불가능할 때만 고정 KST timezone을 반환합니다.
def _korea_timezone():
    try:
        zoneinfo = import_module("zoneinfo")
        return zoneinfo.ZoneInfo(KOREA_ZONE_NAME)
    except Exception:
        return timezone(timedelta(hours=9), "KST")


# 함수 설명: `_date_delta()`는 delta 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _date_delta(date_text: str, days: int) -> str:
    try:
        base = datetime.strptime(str(date_text), "%Y%m%d")
    except Exception:
        base = datetime.now(_korea_timezone())
    return (base + timedelta(days=days)).strftime("%Y%m%d")


# 함수 설명: `_unique_dates()`는 dates의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _unique_dates(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_production_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _production_rows(work_dates: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for work_date in work_dates:
        for product_index, _product in enumerate(PRODUCTS):
            for process_name in _production_processes_for_product(product_index):
                if work_date == "20260627" and process_name == "W/B1":
                    continue
                process_index = _process_index(process_name)
                quantity = _quantity_value(work_date, product_index, process_index, base=180)
                rows.append(_product_process_row(work_date, product_index, process_index, PRODUCTION=quantity))
        rows.extend(_validation_production_rows(work_date))
    return rows


# 함수 설명: `_wip_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _wip_rows(work_dates: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for work_date in work_dates:
        for product_index, _product in enumerate(PRODUCTS):
            for process_name in _wip_processes_for_product(product_index):
                if work_date == "20260626" and process_name == "W/B6":
                    continue
                process_index = _process_index(process_name)
                quantity = _quantity_value(work_date, product_index, process_index, base=35)
                rows.append(
                    _product_process_row(
                        work_date,
                        product_index,
                        process_index,
                        WIP=quantity,
                        LOT_ID=f"L{product_index + 1:03d}-{process_name.replace('/', '')}",
                        SNAPSHOT_TIME="07:00",
                    )
                )
        rows.extend(_validation_wip_rows(work_date))
    return rows


# 함수 설명: `_validation_production_rows()`는 production·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _validation_production_rows(work_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if work_date == "20260630":
        rows.append(
            _scenario_row(
                work_date,
                2,
                "PKG OUT",
                "PRODUCTION",
                420,
                TECH="1D",
                DENSITY="24G",
                DEN="24G",
                MODE="LPDDR5X",
                LEAD="200",
                MCP_NO="",
                DEVICE="DEV-MOBILE-PKGOUT-B",
                DEVICE_DESC="LPDDR5X second mobile PKG OUT sample",
            )
        )
        rows.append(_scenario_row(work_date, 1, "FCB/H", "PRODUCTION", 17))
        rows.append(
            _scenario_row(
                work_date,
                0,
                "FCB/H",
                "PRODUCTION",
                0,
                MCP_NO="Z-FCBH0",
                DEVICE="DEV-FCBH-ZERO",
                DEVICE_DESC="zero-production FCB/H control",
            )
        )
        rows.extend(
            [
                _scenario_row(
                    work_date,
                    6,
                    "FCB1",
                    "PRODUCTION",
                    2000,
                    ORG="8",
                    DEVICE="DEV-SP-DECOY-X8",
                    DEVICE_DESC="SP 16G DDR5 1ST X8 78 FCBGA SDP decoy",
                ),
                _scenario_row(
                    work_date,
                    6,
                    "FCB1",
                    "PRODUCTION",
                    2100,
                    LEAD="96",
                    DEVICE="DEV-SP-DECOY-LEAD96",
                    DEVICE_DESC="SP 16G DDR5 2ND X4 96 FCBGA SDP decoy",
                ),
                _scenario_row(
                    work_date,
                    6,
                    "FCB1",
                    "PRODUCTION",
                    2200,
                    PKG1="VFBGA",
                    PKG_TYPE1="VFBGA",
                    DEVICE="DEV-SP-DECOY-VFBGA",
                    DEVICE_DESC="SP 16G DDR5 2ND X4 78 VFBGA SDP decoy",
                ),
                _scenario_row(
                    work_date,
                    7,
                    "SBM",
                    "PRODUCTION",
                    999,
                    MCP_NO="L-218K8H-A",
                    DEVICE="DEV-L218-PREFIX-DECOY",
                    DEVICE_DESC="L-218K8H-A exact-match decoy",
                ),
            ]
        )
    if work_date == "20260701":
        rows.extend(_bg_decoy_rows(work_date, "PRODUCTION", [5000, 5100, 5200]))
        rows.extend(
            [
                _scenario_row(
                    work_date,
                    0,
                    "INPUT",
                    "PRODUCTION",
                    50,
                    **_zero_semantics_identity("DA-WIP", "Z-DA0"),
                ),
                _scenario_row(
                    work_date,
                    0,
                    "INPUT",
                    "PRODUCTION",
                    0,
                    **_zero_semantics_identity("INPUT", "Z-IN0"),
                ),
            ]
        )
    if work_date == "20260624":
        for index in range(6):
            rows.append(
                _scenario_row(
                    work_date,
                    0,
                    "INPUT",
                    "PRODUCTION",
                    100,
                    **_rank_identity(index),
                )
            )
    return rows


# 함수 설명: `_validation_wip_rows()`는 WIP·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _validation_wip_rows(work_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if work_date == "20260630":
        hbm_processes = ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6", "FCB1", "FCB2", "FCB/H"]
        hbm_values = [40, 45, 50, 55, 60, 65, 70, 75, 80]
        for process_name, quantity in zip(hbm_processes, hbm_values, strict=True):
            rows.append(
                _scenario_row(
                    work_date,
                    1,
                    process_name,
                    "WIP",
                    quantity,
                    MCP_NO="H-002",
                    DEVICE="DEV-HBM-B",
                    DEVICE_DESC="HBM3E second product sample",
                    SNAPSHOT_TIME="07:00",
                )
            )
        rows.append(
            _scenario_row(
                work_date,
                4,
                "D/A1",
                "WIP",
                999,
                SNAPSHOT_TIME="12:00",
                LOT_ID="L-DA-GDDR6-NOON",
            )
        )
    if work_date == "20260701":
        rows.extend(_bg_decoy_rows(work_date, "WIP", [4000, 4100, 4200]))
        rows.append(
            _scenario_row(
                work_date,
                0,
                "D/A1",
                "WIP",
                0,
                **_zero_semantics_identity("DA-WIP", "Z-DA0"),
                SNAPSHOT_TIME="07:00",
            )
        )
    if work_date == "20260624":
        totals = [1200, 1000, 800, 600, 400, 200]
        for index, total in enumerate(totals):
            for process_name in ("D/S1", "D/A1"):
                rows.append(
                    _scenario_row(
                        work_date,
                        0,
                        process_name,
                        "WIP",
                        total // 2,
                        **_rank_identity(index),
                        SNAPSHOT_TIME="07:00",
                    )
                )
    return rows


# 함수 설명: `_scenario_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _scenario_row(
    work_date: str,
    product_index: int,
    process_name: str,
    quantity_field: str,
    quantity: int | float,
    **overrides: Any,
) -> dict[str, Any]:
    row = _product_process_row(work_date, product_index, _process_index(process_name))
    row[quantity_field] = quantity
    row.update(overrides)
    return row


# 함수 설명: `_bg_decoy_rows()`는 decoy·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _bg_decoy_rows(work_date: str, quantity_field: str, quantities: list[int]) -> list[dict[str, Any]]:
    variants = [
        {"LEAD": "78", "DEVICE": "DEV-RG-DECOY-LEAD78", "DEVICE_DESC": "RG 32G DDR4 FBGA 78 DDP decoy"},
        {"DENSITY": "16G", "DEN": "16G", "DEVICE": "DEV-RG-DECOY-DEN16", "DEVICE_DESC": "RG 16G DDR4 FBGA 96 DDP decoy"},
        {"PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "DEVICE": "DEV-RG-DECOY-FCBGA", "DEVICE_DESC": "RG 32G DDR4 FCBGA 96 DDP decoy"},
    ]
    return [
        _scenario_row(work_date, 5, "B/G1", quantity_field, quantity, **variant)
        for variant, quantity in zip(variants, quantities, strict=True)
    ]


# 함수 설명: `_rank_identity()`는 식별자의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다.
def _rank_identity(index: int) -> dict[str, Any]:
    number = index + 1
    return {
        "FAMILY": "VALIDATION",
        "TECH": f"RK{number}",
        "DENSITY": "8G",
        "DEN": "8G",
        "MODE": "RANK",
        "ORG": "TEST",
        "PKG1": "BGA",
        "PKG_TYPE1": "BGA",
        "PKG2": "RANK",
        "PKG_TYPE2": "RANK",
        "LEAD": str(100 + number),
        "MCP_NO": f"R-{number:03d}",
        "DEVICE": f"DEV-RANK-{number}",
        "DEVICE_DESC": f"ranking validation product {number}",
    }


# 함수 설명: `_zero_semantics_identity()`는 0값이 미집계인지 실제 0인지 구분하는 dummy 검증용 제품 식별자를 만듭니다.
def _zero_semantics_identity(label: str, mcp_no: str) -> dict[str, Any]:
    return {
        "FAMILY": "VALIDATION",
        "TECH": "ZW",
        "DENSITY": "4G",
        "DEN": "4G",
        "MODE": "ZERO",
        "ORG": "TEST",
        "PKG1": "BGA",
        "PKG_TYPE1": "BGA",
        "PKG2": "ZERO",
        "PKG_TYPE2": "ZERO",
        "LEAD": "44",
        "MCP_NO": mcp_no,
        "DEVICE": f"DEV-ZERO-{label}",
        "DEVICE_DESC": f"zero semantics {label} control",
    }


# 함수 설명: `_production_processes_for_product()`는 지정 제품의 dummy 생산 실적이 존재하는 세부 공정 목록을 반환합니다.
def _production_processes_for_product(product_index: int) -> list[str]:
    da_steps = ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]
    wb_steps = ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"]
    common = ["INPUT", *da_steps, "D/S1", *wb_steps, "FCB1", "FCB/H", "B/G1", "SBM", "PKG OUT"]
    if product_index == 1:
        return ["INPUT", *wb_steps, "FCB1", "FCB2", "FCB/H"]
    if product_index == 3:
        return ["INPUT", "D/A1", "B/G1", "B/G2"]
    if product_index == 4:
        return ["INPUT", "B/G1", "B/G2"]
    if product_index == 5:
        return ["INPUT", "B/G1", "B/G2"]
    if product_index == 6:
        return ["INPUT", "FCB1", "FCB2", "FCB/H"]
    if product_index == 7:
        return ["INPUT", "SBM", "D/A1"]
    return common


# 함수 설명: `_wip_processes_for_product()`는 지정 제품의 dummy 재공이 존재하는 세부 공정 목록을 반환합니다.
def _wip_processes_for_product(product_index: int) -> list[str]:
    da_steps = ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]
    wb_steps = ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"]
    common = [*da_steps, "D/S1", *wb_steps, "FCB1", "FCB/H", "B/G1"]
    if product_index == 1:
        return [*wb_steps, "FCB1", "FCB2", "FCB/H"]
    if product_index == 3:
        return ["B/G1", "B/G2"]
    if product_index == 4:
        return ["D/A1"]
    if product_index == 5:
        return ["B/G1", "B/G2"]
    if product_index == 6:
        return ["FCB1", "FCB/H"]
    if product_index == 7:
        return ["SBM"]
    return common


# 함수 설명: `_process_index()`는 공정명을 정렬·fixture 계산에 사용할 안정적인 순번으로 변환합니다.
def _process_index(oper_name: str) -> int:
    for index, process in enumerate(PROCESSES):
        if process["OPER_NAME"] == oper_name or process["OPER"] == oper_name:
            return index
    return 0


# 함수 설명: `_quantity_value()`는 제품·공정·일자 조합에서 재현 가능한 dummy 수량 값을 계산합니다.
def _quantity_value(work_date: str, product_index: int, process_index: int, base: int) -> int:
    date_factor = int(str(work_date)[-2:]) if str(work_date)[-2:].isdigit() else 1
    return base + product_index * 37 + process_index * 11 + date_factor


# 함수 설명: `_target_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
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


# 함수 설명: `_product_token_fixture_rows()`는 token·fixture·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _product_token_fixture_rows(work_dates: list[str]) -> list[dict[str, Any]]:
    fixtures = [
        {"FAMILY": "DDR", "TECH": "RG", "DENSITY": "8G", "DEN": "8G", "MODE": "DDR4", "ORG": "16", "PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "96", "MCP_NO": "L-218K8H", "DEVICE": "RG-X16", "DEVICE_DESC": "RG 8G DDR4 X16 96 FCBGA SDP", "WIP": 10, "PRODUCTION": 100},
        {"FAMILY": "DDR", "TECH": "CP", "DENSITY": "16G", "DEN": "16G", "MODE": "DDR", "ORG": "8", "PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "78", "MCP_NO": "L-216A1", "DEVICE": "CP-X8", "DEVICE_DESC": "CP 16G DDR X8 78 FCBGA SDP", "WIP": 20, "PRODUCTION": 200},
        {"FAMILY": "DDR", "TECH": "CP", "DENSITY": "16G", "DEN": "16G", "MODE": "DDR", "ORG": "16", "PKG1": "VFBGA", "PKG_TYPE1": "VFBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "78", "MCP_NO": "A-663Z9", "DEVICE": "CP-F78-V", "DEVICE_DESC": "CP 16G DDR X16 78 VFBGA SDP", "WIP": 30, "PRODUCTION": 300},
        {"FAMILY": "DDR", "TECH": "RG", "DENSITY": "8G", "DEN": "8G", "MODE": "DDR4", "ORG": "16", "PKG1": "VFBGA", "PKG_TYPE1": "VFBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "96", "MCP_NO": "A-777Z9", "DEVICE": "RG-F96-V", "DEVICE_DESC": "RG 8G DDR4 X16 96 VFBGA SDP", "WIP": 35, "PRODUCTION": 350},
        {"FAMILY": "DDR", "TECH": "RG", "DENSITY": "8G", "DEN": "8G", "MODE": "DDR4", "ORG": "8", "PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "96", "MCP_NO": "L-999", "DEVICE": "RG-WRONG-ORG", "DEVICE_DESC": "RG 8G DDR4 X8 96 FCBGA SDP", "WIP": 40, "PRODUCTION": 400},
        {"FAMILY": "DDR", "TECH": "CP", "DENSITY": "16G", "DEN": "16G", "MODE": "DDR", "ORG": "8", "PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "96", "MCP_NO": "L-000", "DEVICE": "CP-WRONG-LEAD", "DEVICE_DESC": "CP 16G DDR X8 96 FCBGA SDP", "WIP": 50, "PRODUCTION": 500},
        {"FAMILY": "DDR", "TECH": "SP", "DENSITY": "16G", "DEN": "16G", "MODE": "DDR5", "ORG": "4", "PKG1": "FCBGA", "PKG_TYPE1": "FCBGA", "PKG2": "SDP", "PKG_TYPE2": "SDP", "LEAD": "78", "MCP_NO": "", "DEVICE": "DEV-SP-DDR5-FCBGA78", "DEVICE_DESC": "SP 16G DDR5 2ND X4 78 FCBGA SDP", "WIP": 60, "PRODUCTION": 600},
        {"FAMILY": "TEST", "TECH": "ZZ", "DENSITY": "4G", "DEN": "4G", "MODE": "SDR", "ORG": "4", "PKG1": "BGA", "PKG_TYPE1": "BGA", "PKG2": "NONE", "PKG_TYPE2": "NONE", "LEAD": "12", "MCP_NO": "Z-000", "DEVICE": "NO-MATCH", "DEVICE_DESC": "negative control", "WIP": 99, "PRODUCTION": 999},
    ]
    processes = [
        {"OPER": "WB1", "OPER_NAME": "W/B1", "OPER_SEQ": "200"},
        {"OPER": "INPUT", "OPER_NAME": "INPUT", "OPER_SEQ": "010"},
    ]
    rows: list[dict[str, Any]] = []
    for work_date in work_dates:
        for fixture in fixtures:
            for process in processes:
                row = {
                    "WORK_DATE": work_date,
                    "WORK_DT": work_date,
                    "DATE": work_date,
                    "SHIFT": "1",
                    "FACTORY": "PNT",
                    "FAB": "PKG",
                    "OPER": process["OPER"],
                    "OPER_NUM": process["OPER"],
                    "OPER_NAME": process["OPER_NAME"],
                    "OPER_NM": process["OPER_NAME"],
                    "OPER_SEQ": process["OPER_SEQ"],
                    "TSV_DIE_TYP": "",
                    "TSV_DIE_TYPE": "",
                }
                row.update(fixture)
                rows.append(row)
    return rows


# 함수 설명: `_equipment_assign_rows()`는 assign·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _equipment_assign_rows() -> list[dict[str, Any]]:
    rows = []
    models = ["EQM-A", "EQM-HBM", "EQM-MOBILE", "EQM-FCB", "EQM-BG"]
    press_counts = [2, 4, 1, 3, 2]
    pkg_sizes = ["12x12", "18x18", "10x10", "14x14", "16x16"]
    for index, product in enumerate(PRODUCTS):
        process = PROCESSES[index % len(PROCESSES)]
        model = models[index % len(models)]
        rows.append(
            {
                "BAY_ID": f"BAY{index + 1:02d}",
                "EQUIP_ID": f"EQP{index + 1:03d}",
                "EQUIP_MODEL": model,
                "PRESS_CNT": press_counts[index % len(press_counts)],
                "OPER": process["OPER"],
                "OPER_NM": process["OPER_NAME"],
                "MODE": product["MODE"],
                "DENSITY": product["DENSITY"],
                "TECH": product["TECH"],
                "PKG1": product["PKG1"],
                "PKG2": product["PKG2"],
                "LEAD": product["LEAD"],
                "ORG": product["ORG"],
                "PKGSIZE": pkg_sizes[index % len(pkg_sizes)],
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


# 함수 설명: `_eqp_uph_rows()`는 UPH·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _eqp_uph_rows() -> list[dict[str, Any]]:
    rows = []
    models = ["EQM-A", "EQM-HBM", "EQM-MOBILE", "EQM-FCB", "EQM-BG"]
    press_counts = [2, 4, 1, 3, 2]
    uph_values = [123.4, 88.2, 156.7, 112.0, 97.5]
    for index, product in enumerate(PRODUCTS):
        process = PROCESSES[index % len(PROCESSES)]
        model = models[index % len(models)]
        rows.append(
            {
                "EQUIP_MODEL": model,
                "OPER": process["OPER"],
                "OPER_NAME": process["OPER_NAME"],
                "PRESS_CNT": press_counts[index % len(press_counts)],
                "MODE": product["MODE"],
                "TECH": product["TECH"],
                "ORG": product["ORG"],
                "DENSITY": product["DENSITY"],
                "PKG1": product["PKG1"],
                "PKG2": product["PKG2"],
                "LEAD": product["LEAD"],
                "MCP_NO": product["MCP_NO"],
                "RECIPE_ID": f"RCP-{index + 1:03d}",
                "UPH": uph_values[index % len(uph_values)],
                "LOAD_DT": "20260701",
                "BASE_DT": "20260701",
                "EQP_MODEL": model,
                "DEN": product["DENSITY"],
                "PKG_TYPE1": product["PKG1"],
                "PKG_TYPE2": product["PKG2"],
                "OPER_NUM": process["OPER"],
            }
        )
    hbm_alternate = deepcopy(rows[1])
    hbm_alternate.update(
        {
            "OPER": "DA2",
            "OPER_NAME": "D/A2",
            "OPER_NUM": "DA2",
            "RECIPE_ID": "RCP-HBM-ALT",
            "UPH": 101.8,
        }
    )
    rows.append(hbm_alternate)
    return rows


# 함수 설명: `_lot_status_rows()`는 상태·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _lot_status_rows() -> list[dict[str, Any]]:
    return [
        _lot_row(_product_process_row("20260701", 0, 0), "T1234567GEN1", "OnHold", "WAITING", 100, 25, 12.5, 40.0, "검증용 HOLD"),
        _lot_row(_product_process_row("20260701", 1, 1), "T7654321GEN1", "NotOnHold", "RUNNING", 80, 20, 5.0, 25.0, ""),
        _lot_row(_product_process_row("20260701", 2, 2), "T2222222GEN1", "NotOnHold", "WAITING", 60, 18, 3.5, 18.0, ""),
        _lot_row(_product_process_row("20260701", 2, 3), "T2222223GEN1", "NotOnHold", "RUNNING", 70, 16, 2.0, 11.0, ""),
    ]


# 함수 설명: `_hold_history_rows()`는 history·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _hold_history_rows() -> list[dict[str, Any]]:
    return [
        {
            **_hold_row(_product_process_row("20260701", 0, 0), "T1234567GEN1", "H000", "검증용 이전 HOLD 이력"),
            "HOLD_TM": "2026-06-30 18:00:00",
        },
        _hold_row(_product_process_row("20260701", 0, 0), "T1234567GEN1", "H001", "검증용 HOLD 이력"),
        _hold_row(_product_process_row("20260701", 1, 1), "T7654321GEN1", "H002", "레시피 확인 HOLD"),
    ]


# 함수 설명: `_product_process_row()`는 process·행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
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


# 함수 설명: `_lot_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _lot_row(base: dict[str, Any], lot_id: str, hold_stat: str, lot_stat: str, prod_qty: int, wf_qty: int, in_tat: float, cum_tat: float, hold_reason: str) -> dict[str, Any]:
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


# 함수 설명: `_hold_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
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


# 함수 설명: `_apply_params()`는 더미 행에 날짜·공정·제품 등 조회 파라미터 조건을 적용합니다.
def _apply_params(rows: list[dict[str, Any]], params: Any) -> list[dict[str, Any]]:
    if not isinstance(params, dict):
        return rows
    filtered = rows
    for field, value in params.items():
        if value in (None, "", [], {}):
            continue
        filtered = _filter_rows(filtered, str(field), [value], "eq", keep_if_missing=True)
    return filtered


# 함수 설명: `_apply_filters()`는 더미 행에 표준 field/operator/value 필터 조건을 순서대로 적용합니다.
def _apply_filters(rows: list[dict[str, Any]], filters: Any) -> list[dict[str, Any]]:
    if isinstance(filters, list):
        items = [(condition.get("field"), condition) for condition in filters if isinstance(condition, dict)]
    elif isinstance(filters, dict):
        items = list(filters.items())
    else:
        return rows
    filtered = rows
    for field, condition in items:
        if not field:
            continue
        if isinstance(condition, dict):
            values = condition.get("values", condition.get("value", []))
            operator = condition.get("operator", condition.get("op", "eq"))
        else:
            values = condition
            operator = "eq"
        if not isinstance(values, list):
            values = [values]
        filtered = _filter_rows(filtered, str(field), values, str(operator), keep_if_missing=True)
    return filtered


# 함수 설명: `_filter_rows()`는 조건과 우선순위에 맞는 행 목록만 골라 원래 순서를 유지해 반환합니다.
def _filter_rows(rows: list[dict[str, Any]], field: str, values: list[Any], operator: str, keep_if_missing: bool) -> list[dict[str, Any]]:
    candidates = _field_candidates(field)
    if not any(any(candidate in row for candidate in candidates) for row in rows):
        return rows if keep_if_missing else []
    normalized_values = {_normalize(value) for value in values}
    if operator in {"eq", "in", "="}:
        return [row for row in rows if any(_normalize(row.get(candidate)) in normalized_values for candidate in candidates if candidate in row)]
    if operator in {"not_in", "ne", "!="}:
        return [row for row in rows if all(_normalize(row.get(candidate)) not in normalized_values for candidate in candidates if candidate in row)]
    if operator in {"contains", "like"}:
        return [row for row in rows if any(any(value in _normalize(row.get(candidate)) for value in normalized_values) for candidate in candidates if candidate in row)]
    return rows


# 함수 설명: `_field_candidates()`는 표준 필터 field에 대응할 수 있는 실제 컬럼 alias 후보를 반환합니다.
def _field_candidates(field: str) -> list[str]:
    aliases = {
        "DATE": ["DATE", "WORK_DATE", "WORK_DT", "LOAD_DT", "BASE_DT"],
        "WORK_DATE": ["WORK_DATE", "WORK_DT", "DATE"],
        "MODE": ["MODE", "Mode"],
        "DEN": ["DEN", "DENSITY"],
        "PKG_TYPE1": ["PKG_TYPE1", "PKG1"],
        "PKG_TYPE2": ["PKG_TYPE2", "PKG2"],
        "MCP_NO": ["MCP_NO", "MCP NO"],
        "TSV_DIE_TYP": ["TSV_DIE_TYP", "TSV_DIE_TYPE"],
        "OPER_NUM": ["OPER_NUM", "OPER"],
        "OPER_NAME": ["OPER_NAME", "OPER_NM"],
        "EQP_ID": ["EQP_ID", "EQUIP_ID"],
        "EQP_MODEL": ["EQP_MODEL", "EQUIP_MODEL", "EQPIP_MODEL"],
    }
    return aliases.get(field, [field])


# 함수 설명: `_normalize()`는 normalize의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize(value: Any) -> str:
    text = str(value if value is not None else "").strip().upper()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return text


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DummyDataRetriever(Component):
    display_name = "08 더미 데이터 조회기"
    description = "실제 소스 조회가 꺼져 있을 때 데이터 조회 작업을 data_catalog 구조와 같은 더미 행으로 실행합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

    # Langflow 출력 함수: '조회 페이로드 (retrieval_payload)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=retrieve_dummy_data(getattr(self, "payload", None)))
