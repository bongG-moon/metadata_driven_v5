# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 실시간 생산 판정 더미 데이터 생성기
# 역할: 실시간 생산 Report 예시를 실행할 수 있도록 판정 컬럼이 포함된 결정론적 더미 Snapshot을 생성합니다.
# 주요 입력: 생성 행 수, 난수 seed, WORK_DATE, 공정명 목록
# 주요 출력: production.judgement.dataset.v1 Data
# 처리 흐름: 제품 master 생성 -> 공정/제품 Case 생성 -> 수치 계산 -> 판정값 생성 -> Snapshot 계약 반환
# 유지보수 포인트: 운영 전환 시 이 노드만 실제 판정 데이터 로더로 교체하고 다음 Report 노드의 dataset 계약은 유지합니다.
# =============================================================================

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import Output, StrInput
from lfx.schema.data import Data


CONTRACT_VERSION = "production.judgement.dataset.v1"
DEFAULT_ROW_COUNT = 500
MAX_ROW_COUNT = 5_000
DEFAULT_SEED = 20260727
DEFAULT_PROCESSES = "W/B1,W/B2,W/B3,W/B4"
KST = timezone(timedelta(hours=9))

COLUMNS = [
    "WORK_DATE",
    "MODE",
    "DENSITY",
    "TECH",
    "ORG",
    "PKG1",
    "PKG2",
    "LEAD",
    "MCP_NO",
    "OPER",
    "OPER_NAME",
    "OPER_SEQ",
    "NETDIE_300_CNT",
    "PRODUCTION",
    "WIP",
    "INPUT_PLAN",
    "OUT_PLAN",
    "생산실적달성율",
    "달성율*판정",
    "적정재공수량",
    "적정재공율",
    "적정재공*판정",
    "EQP_COUNT",
    "DOWN_CNT",
    "OVER_2H_DOWN",
    "기준UPH",
    "보유UPH",
    "보유CAPA(24H)",
    "보유CAPA(잔여)",
    "잔여목표수량",
    "CAPA확보율",
    "장비BAL",
    "CAPA판정",
    "CAPA이상판단",
    "이전공정재공",
    "현재작업재공",
    "장비교체판단재공",
    "재공보유율",
    "장비교체판단",
    "장비필요대수",
    "평균가동율",
    "평균NOWIP",
    "가동율목표",
    "가동율달성률",
    "가동율판정",
]


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: `_bounded_int()`는 INT이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(float(_text(value))) if _text(value) else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_process_names()`는 00 실시간 생산 판정 더미 데이터 처리 중 names 관련 값을 계산·변환하는 내부 helper입니다.
def _process_names(value: Any) -> list[str]:
    names: list[str] = []
    for item in _text(value).replace(";", ",").replace("\n", ",").split(","):
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names[:20] or ["W/B1", "W/B2", "W/B3", "W/B4"]


# 함수 설명: `_work_date()`는 00 실시간 생산 판정 더미 데이터 처리 중 날짜 관련 값을 계산·변환하는 내부 helper입니다.
def _work_date(value: Any) -> str:
    candidate = _text(value)
    if candidate:
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return datetime.now(KST).strftime("%Y-%m-%d")


# 함수 설명: `_round()`는 00 실시간 생산 판정 더미 데이터 처리 중 round 관련 값을 계산·변환하는 내부 helper입니다.
def _round(value: float, digits: int = 1) -> float:
    return round(float(value), digits)


# 함수 설명: `_product_master()`는 00 실시간 생산 판정 더미 데이터 처리 중 master 관련 값을 계산·변환하는 내부 helper입니다.
def _product_master(index: int, rng: random.Random) -> dict[str, Any]:
    densities = ("8G", "16G", "24G", "32G")
    techs = ("1A", "1B", "1C")
    orgs = ("X4", "X8", "X16")
    pkg1s = ("FBGA", "WLCSP", "QFN")
    pkg2s = ("STD", "SLIM", "STACK")
    leads = ("A-663", "B-123", "L-218", "FC78")
    return {
        "MODE": ("MASS", "RAMP", "EVAL")[index % 3],
        "DENSITY": densities[index % len(densities)],
        "TECH": techs[(index // 2) % len(techs)],
        "ORG": orgs[(index // 3) % len(orgs)],
        "PKG1": pkg1s[(index // 5) % len(pkg1s)],
        "PKG2": pkg2s[(index // 7) % len(pkg2s)],
        "LEAD": leads[(index // 11) % len(leads)],
        "MCP_NO": f"MCP-{1000 + index:04d}-{rng.choice(('A', 'B', 'C'))}",
    }


# 함수 설명: `_achievement_case()`는 00 실시간 생산 판정 더미 데이터 처리 중 Function Case 관련 값을 계산·변환하는 내부 helper입니다.
def _achievement_case(index: int) -> str:
    # 500행 기준 정상 310, 초과 75, 생산부족 85, Abnormal 30 수준의 안정적인 분포를 만듭니다.
    marker = index % 100
    if marker < 62:
        return "정상"
    if marker < 77:
        return "정상(초과생산)"
    if marker < 94:
        return "생산부족"
    return "Abnormal"


# 함수 설명: `_shortage_flags()`는 00 실시간 생산 판정 더미 데이터 처리 중 flags 관련 값을 계산·변환하는 내부 helper입니다.
def _shortage_flags(index: int) -> tuple[bool, bool, bool]:
    # 생산부족 Case에 3개 원인과 복합 원인이 모두 나타나도록 고정 pattern을 사용합니다.
    marker = index % 10
    return (
        marker in {0, 1, 2, 6, 9},
        marker in {3, 4, 6, 7},
        marker in {5, 7, 8, 9},
    )


# 함수 설명: `_capa_anomaly()`는 00 실시간 생산 판정 더미 데이터 처리 중 anomaly 관련 값을 계산·변환하는 내부 helper입니다.
def _capa_anomaly(
    *,
    achievement: str,
    equipment_count: int,
    wip: int,
    capa_shortage: bool,
    utilization_low: bool,
    index: int,
) -> str:
    if equipment_count == 0 and wip > 0:
        return "Abnormal"
    if capa_shortage:
        return "CAPA부족"
    if achievement == "생산부족" and utilization_low:
        return "생산부진2" if index % 2 else "생산부진1"
    if achievement == "생산부족" and index % 5 == 0:
        return "생산부진1"
    return "정상"


# 함수 설명: `_equipment_judgement()`는 00 실시간 생산 판정 더미 데이터 처리 중 judgement 관련 값을 계산·변환하는 내부 helper입니다.
def _equipment_judgement(
    *,
    capa_anomaly: str,
    equipment_count: int,
    previous_wip: int,
    equipment_balance: float,
    down_count: int,
    index: int,
) -> tuple[str, int]:
    if capa_anomaly not in {"생산부진1", "생산부진2", "CAPA부족"}:
        return ("교체불필요" if index % 4 == 0 else "정상"), 0
    if previous_wip > 0 and (equipment_count <= 2 or index % 7 == 0):
        return "장비필요", max(1, round(previous_wip / 800))
    if equipment_balance < 0.9 or down_count > 0 or index % 5 == 0:
        return "교체필요", max(1, round((1.0 - min(equipment_balance, 1.0)) * max(equipment_count, 1)))
    return "교체불필요", 0


# 함수 설명: `build_dummy_production_dataset()`는 dummy·production·데이터셋 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def build_dummy_production_dataset(
    *,
    row_count: Any = DEFAULT_ROW_COUNT,
    seed: Any = DEFAULT_SEED,
    work_date: Any = "",
    process_names: Any = DEFAULT_PROCESSES,
    snapshot_at: str = "",
) -> dict[str, Any]:
    count = _bounded_int(row_count, DEFAULT_ROW_COUNT, 10, MAX_ROW_COUNT)
    seed_value = _bounded_int(seed, DEFAULT_SEED, 0, 2_147_483_647)
    processes = _process_names(process_names)
    report_date = _work_date(work_date)
    rng = random.Random(seed_value)
    product_count = max(40, min(200, (count + len(processes) - 1) // len(processes)))
    products = [_product_master(index, rng) for index in range(product_count)]
    rows: list[dict[str, Any]] = []

    for index in range(count):
        product = dict(products[index % product_count])
        process_index = (index // product_count) % len(processes)
        process_name = processes[process_index]
        achievement = _achievement_case(index)
        out_plan = 0 if achievement == "Abnormal" else rng.randrange(800, 4_800, 20)
        input_plan = 0 if achievement == "Abnormal" else round(out_plan * rng.uniform(0.9, 1.12))
        if achievement == "정상":
            production = round(out_plan * rng.uniform(0.92, 1.08))
        elif achievement == "정상(초과생산)":
            production = round(out_plan * rng.uniform(1.12, 1.35))
        elif achievement == "생산부족":
            production = round(out_plan * rng.uniform(0.45, 0.86))
        else:
            production = rng.randrange(0, 360, 10)
        achievement_rate = None if out_plan == 0 else _round(production / out_plan * 100)

        wip_shortage, capa_shortage, utilization_low = (
            _shortage_flags(index) if achievement == "생산부족" else (False, False, False)
        )
        adequate_wip = max(120, round(max(out_plan, 800) * rng.uniform(0.16, 0.28)))
        if wip_shortage:
            wip = round(adequate_wip * rng.uniform(0.25, 0.72))
            wip_judgement = "Abnormal"
        elif achievement == "Abnormal":
            wip = rng.randrange(80, 700, 10)
            wip_judgement = "정상"
        elif index % 13 == 0:
            wip = round(adequate_wip * rng.uniform(1.35, 1.75))
            wip_judgement = "재공과다"
        else:
            wip = round(adequate_wip * rng.uniform(0.85, 1.2))
            wip_judgement = "정상"
        wip_rate = _round(wip / adequate_wip * 100)

        equipment_count = 0 if achievement == "Abnormal" and index % 2 == 0 else rng.randint(2, 9)
        down_count = 0 if equipment_count == 0 else rng.randint(0, min(2, equipment_count))
        over_2h_down = rng.randint(0, down_count) if down_count else 0
        standard_uph = rng.randrange(80, 260, 10)
        holding_uph = standard_uph * max(equipment_count - down_count, 0)
        remaining_target = max(out_plan - production, 0)
        remaining_hours = rng.uniform(5.0, 12.0)
        capa_24h = holding_uph * 24
        remaining_capa = round(holding_uph * remaining_hours)
        if capa_shortage and remaining_target > 0:
            remaining_capa = round(remaining_target * rng.uniform(0.35, 0.82))
        capa_rate = 100.0 if remaining_target == 0 else _round(remaining_capa / remaining_target * 100)
        equipment_balance = _round(rng.uniform(0.65, 1.25), 2)
        if capa_shortage:
            capa_judgement = "CAPA부족"
        elif remaining_target == 0 and equipment_count >= 5:
            capa_judgement = "잉여장비"
        else:
            capa_judgement = "CAPA과다"

        utilization_target = _round(rng.uniform(78, 90))
        if utilization_low:
            avg_utilization = _round(utilization_target * rng.uniform(0.48, 0.82))
            utilization_judgement = "Abnormal"
        else:
            avg_utilization = _round(min(99.5, utilization_target * rng.uniform(0.93, 1.12)))
            utilization_judgement = "정상"
        utilization_rate = _round(avg_utilization / utilization_target * 100)
        avg_nowip = _round(max(0.0, 100.0 - avg_utilization) * rng.uniform(0.25, 0.7))

        previous_wip = max(0, round(wip * rng.uniform(0.7, 1.8)))
        current_work_wip = max(0, round(wip * rng.uniform(0.25, 0.75)))
        swap_judgement_wip = previous_wip + current_work_wip
        wip_holding_rate = _round(swap_judgement_wip / max(adequate_wip, 1) * 100)
        capa_anomaly = _capa_anomaly(
            achievement=achievement,
            equipment_count=equipment_count,
            wip=wip,
            capa_shortage=capa_shortage,
            utilization_low=utilization_low,
            index=index,
        )
        equipment_judgement, equipment_needed = _equipment_judgement(
            capa_anomaly=capa_anomaly,
            equipment_count=equipment_count,
            previous_wip=previous_wip,
            equipment_balance=equipment_balance,
            down_count=down_count,
            index=index,
        )

        row = {
            "WORK_DATE": report_date,
            **product,
            "OPER": f"WB{process_index + 1:02d}0",
            "OPER_NAME": process_name,
            "OPER_SEQ": (process_index + 1) * 10,
            "NETDIE_300_CNT": rng.randrange(200, 1_600, 20),
            "PRODUCTION": production,
            "WIP": wip,
            "INPUT_PLAN": input_plan,
            "OUT_PLAN": out_plan,
            "생산실적달성율": achievement_rate,
            "달성율*판정": achievement,
            "적정재공수량": adequate_wip,
            "적정재공율": wip_rate,
            "적정재공*판정": wip_judgement,
            "EQP_COUNT": equipment_count,
            "DOWN_CNT": down_count,
            "OVER_2H_DOWN": over_2h_down,
            "기준UPH": standard_uph,
            "보유UPH": holding_uph,
            "보유CAPA(24H)": capa_24h,
            "보유CAPA(잔여)": remaining_capa,
            "잔여목표수량": remaining_target,
            "CAPA확보율": capa_rate,
            "장비BAL": equipment_balance,
            "CAPA판정": capa_judgement,
            "CAPA이상판단": capa_anomaly,
            "이전공정재공": previous_wip,
            "현재작업재공": current_work_wip,
            "장비교체판단재공": swap_judgement_wip,
            "재공보유율": wip_holding_rate,
            "장비교체판단": equipment_judgement,
            "장비필요대수": equipment_needed,
            "평균가동율": avg_utilization,
            "평균NOWIP": avg_nowip,
            "가동율목표": utilization_target,
            "가동율달성률": utilization_rate,
            "가동율판정": utilization_judgement,
        }
        rows.append({column: row.get(column) for column in COLUMNS})

    generated_at = snapshot_at or datetime.now(KST).replace(microsecond=0).isoformat()
    return {
        "contract_version": CONTRACT_VERSION,
        "source_type": "dummy",
        "snapshot_id": f"dummy:{report_date}:{seed_value}:{count}",
        "snapshot_at": generated_at,
        "work_date": report_date,
        "processes": processes,
        "columns": list(COLUMNS),
        "row_count": len(rows),
        "rows": rows,
        "generation": {
            "seed": seed_value,
            "requested_row_count": count,
            "product_master_count": product_count,
            "rules_version": "dummy.production.judgement.v1",
        },
    }


# Langflow 컴포넌트 클래스: 설정한 작업일자·공정 목록을 기준으로 판정 컬럼이 완비된 더미 생산 데이터를 생성합니다.
class DummyProductionJudgementData(Component):
    display_name = "00 실시간 생산 판정 더미 데이터"
    description = "실시간 생산 분석 Report 예시용 판정 데이터 약 500행을 결정론적으로 생성합니다."
    name = "DummyProductionJudgementData"
    icon = "DatabaseZap"
    inputs = [
        StrInput(
            name="row_count",
            display_name="더미 행 수",
            info="10~5,000 범위입니다. 예시는 500행을 권장합니다.",
            value=str(DEFAULT_ROW_COUNT),
            required=False,
            advanced=False,
        ),
        StrInput(
            name="seed",
            display_name="더미 난수 Seed",
            info="같은 입력이면 같은 판정 데이터가 만들어집니다.",
            value=str(DEFAULT_SEED),
            required=False,
            advanced=False,
        ),
        StrInput(
            name="work_date",
            display_name="작업일자(WORK_DATE)",
            info="비우면 실행일을 사용합니다. YYYY-MM-DD 형식을 권장합니다.",
            value="",
            required=False,
            advanced=False,
        ),
        StrInput(
            name="process_names",
            display_name="분석 공정 목록",
            info="쉼표로 구분합니다. 예: W/B1,W/B2,W/B3,W/B4",
            value=DEFAULT_PROCESSES,
            required=False,
            advanced=False,
        ),
    ]
    outputs = [
        Output(
            name="dataset",
            display_name="판정 데이터",
            method="build_dataset",
            types=["Data"],
        )
    ]

    # 함수 설명: `build_dataset()`는 데이터셋 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
    def build_dataset(self) -> Data:
        payload = build_dummy_production_dataset(
            row_count=getattr(self, "row_count", DEFAULT_ROW_COUNT),
            seed=getattr(self, "seed", DEFAULT_SEED),
            work_date=getattr(self, "work_date", ""),
            process_names=getattr(self, "process_names", DEFAULT_PROCESSES),
        )
        self.status = f"더미 판정 데이터 {payload['row_count']:,}행"
        return Data(data=payload)
