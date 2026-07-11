# Data Analysis Flow v5 검증 질문 및 기대 결과

작성 기준일: 2026-07-10  
결정적 검증 기준일: `20260701`  
기본 조회 모드: `04A.retrieval_mode=dummy`

## 1. 목적과 범위

이 문서는 `validation_questions.txt`의 25개 질문 중 신규 확장 질문 14~25의 데이터 계약과 합격 기준을 고정합니다.

- 1~13: 기존 생산·재공 중심 자동 회귀 질문입니다. 세부 intent와 pandas 기대값은 `tools/validate_representative_questions.py`를 기준으로 유지합니다.
- 14~23: 한 번의 요청으로 자동 검증하는 확장 질문입니다.
- 24~25: 동일한 `session_id`로 선행 질문을 먼저 실행해야 하는 별도 2-turn 검증입니다.
- 아래 행 수와 수량은 현재 `08_dummy_data_retriever.py` fixture 기준입니다. Live 환경에서는 값이 달라질 수 있지만 dataset 선택, grain, 계산식, 0건 처리와 후속질문 동작은 같아야 합니다.

## 2. 자동 검증 확장 질문 14~23

| No. | 질문 | 필수 dataset | 기대 row count와 핵심값 | 검증 목적 |
| ---: | --- | --- | --- | --- |
| 14 | 7월 1일 제품별 INPUT 계획과 OUT 계획을 OUT 계획이 큰 순서로 알려줘 | `target` | 8행. `INPUT_PLAN`, `OUT_PLAN`을 포함하고 `OUT_PLAN` 내림차순. 첫 행은 `M-001`, `OUT_PLAN=1200`; 마지막 행은 `L-218K8H`, `OUT_PLAN=150`. | 계획 dataset 선택, `2026-07-01` 날짜 형식, 계획 컬럼 정규화와 정렬 |
| 15 | 현재 장비 모델별 보유 장비 대수를 알려줘. 장비 ID 중복은 제외해줘 | `equipment_assign` | 5행. `EQM-A=2`, `EQM-HBM=2`, `EQM-MOBILE=2`, `EQM-FCB=1`, `EQM-BG=1`; 전체 distinct 장비는 8대. | `EQUIP_ID`/`EQP_ID` alias 처리와 `nunique` 장비 수 집계 |
| 16 | 장비 모델별 평균 UPH를 낮은 순서로 보여줘 | `eqp_uph` | 5행. 평균 UPH 오름차순은 `EQM-HBM=92.73`, `EQM-BG=97.5`, `EQM-FCB=112.0`, `EQM-A=123.4`, `EQM-MOBILE=156.7`. | 같은 모델의 여러 Recipe 행을 먼저 평균 집계한 뒤 정렬하는지 검증 |
| 17 | 현재 HOLD 중인 LOT 목록과 LOT별 UNIT 수량, Wafer 수량, 현재·누적 TAT를 보여줘 | `lot_status` | 1행. `LOT_ID=T1234567GEN1`, `UNIT_QTY=100`, `WAFER_QTY=25`, `IN_TAT=12.5`, `CUM_TAT=40.0`. | `HOLD_STAT=OnHold` 필터와 `PROD_QTY`→UNIT, `WF_QTY`→Wafer 의미 매핑 |
| 18 | 7월 1일 제품별 INPUT 계획 대비 실제 INPUT 실적과 달성률을 알려줘 | `target`, `production_today` | 계획이 있는 제품 기준 8행. `INPUT_ACTUAL`은 `OPER_NAME=INPUT` 실적 합계. `SHORTFALL=INPUT_PLAN-INPUT_ACTUAL`, `ACHIEVEMENT_RATE=INPUT_ACTUAL/INPUT_PLAN*100`. 예: `DEV001`은 `800`, `181`, `619`, `22.6%`; `DEV-L218K8H`는 `100`, `440`, `-340`, `440.0%`. | `2026-07-01`과 `20260701` 날짜 정규화, 제품 grain join, 계획 없는 실적 제어행 제외, 달성률 계산 |
| 19 | 현재 D/A1 공정에 배정된 장비와 해당 Recipe의 UPH를 함께 보여줘 | `equipment_assign`, `eqp_uph` | 1행. `EQP_ID=EQP002`, `EQP_MODEL=EQM-HBM`, `OPER_NAME=D/A1`, `RECIPE_ID=RCP-002`, `PRESS_CNT=4`, `UPH=88.2`. | 모델명만 사용한 다대다 join을 피하고 공정·Recipe grain으로 정확히 결합하는지 검증 |
| 20 | 현재 HOLD 중인 LOT별 가장 최근 HOLD 코드와 상세 사유를 알려줘 | `lot_status`, `hold_history` | 1행. `T1234567GEN1`의 최신 이력인 `2026-07-01 08:00:00`, `HOLD_CD=H001`, `HOLD_DESC=검증용 HOLD 이력`. 과거 `H000` 행은 최종 결과에서 제외. | 현재 HOLD LOT 집합과 이력 join, LOT별 최신 시각 선택 |
| 21 | 7월 1일 DA 공정 재공이 가장 많은 제품의 현재 LOT 수, UNIT 수량, Wafer 수량을 알려줘 | `wip_today`, `lot_status` | 1행. DA WIP 1위는 `DEV002`, `TOP_DA_WIP=891`. 해당 제품의 `LOT_COUNT=2`(`T2222222GEN1`, `T2222223GEN1`), `UNIT_QTY=130`, `WAFER_QTY=34`. | 1단계 제품별 DA WIP 집계·Top 1 선택 후 그 제품 key로 LOT dataset을 제한하고 `nunique`/합계를 계산하는 순차 분석 |
| 22 | LOT T9999999GEN1의 현재 상태와 TAT를 알려줘 | `lot_status` | 0행. `data.row_count=0`, `data.rows=[]`; 정상적인 조회 결과 없음 메시지. | 필수 LOT_ID 조건과 0건 결과의 정상 종료, 상태·TAT 환각 방지 |
| 23 | 7월 2일 제품별 OUT 계획을 알려줘 | `target` | 0행. Dummy 계획은 `2026-07-01`만 있으므로 7월 1일 행을 대신 반환하면 실패. | 날짜 정규화 후 실제 필터 적용과 0건 처리 |

### 2.1 질문 18의 제품별 기준값

질문 24의 후속 필터를 검증하기 위해 질문 18의 기대값을 다음과 같이 고정합니다.

| DEVICE | INPUT_PLAN | INPUT_ACTUAL | SHORTFALL | ACHIEVEMENT_RATE |
| --- | ---: | ---: | ---: | ---: |
| `DEV001` | 800 | 181 | 619 | 22.6% |
| `DEV-HBM` | 700 | 218 | 482 | 31.1% |
| `DEV002` | 600 | 255 | 345 | 42.5% |
| `DEV-L267` | 500 | 292 | 208 | 58.4% |
| `DEV-DA-GDDR6` | 400 | 329 | 71 | 82.2% |
| `DEV-RG-DDR4` | 300 | 366 | -66 | 122.0% |
| `DEV-SP-DDR5` | 200 | 403 | -203 | 201.5% |
| `DEV-L218K8H` | 100 | 440 | -340 | 440.0% |

## 3. 별도 2-turn 후속질문 24~25

두 검증 모두 Turn 1과 Turn 2에 같은 `session_id`를 사용하고, Turn 1의 세션 상태 저장이 완료된 뒤 Turn 2를 실행합니다.

### 24. 이전 결과에서 달성률 80% 미만만 재사용

Turn 1:

> 7월 1일 제품별 INPUT 계획 대비 실제 INPUT 실적과 달성률을 알려줘

Turn 2:

> 그중 달성률이 80% 미만인 제품만 보여줘

기대 동작:

- Turn 2 intent는 `request_scope=followup_transform`, `reuse_strategy=previous_result`를 사용합니다.
- Turn 2의 신규 `retrieval_jobs`는 비어 있어야 하며 `target`과 `production_today`를 다시 조회하지 않습니다.
- 결과는 4행: `DEV001`, `DEV-HBM`, `DEV002`, `DEV-L267`입니다.
- `INPUT_PLAN`, `INPUT_ACTUAL`, `SHORTFALL`, `ACHIEVEMENT_RATE` 컬럼을 유지합니다.
- “그중”의 범위를 전체 제품이나 새로운 기준일로 초기화하면 실패입니다.

### 25. 이전 HOLD LOT에서 이력 dataset으로 확장

Turn 1:

> 현재 HOLD 중인 LOT 목록과 LOT별 UNIT 수량, Wafer 수량, 현재·누적 TAT를 보여줘

Turn 2:

> 그 HOLD LOT의 HOLD 발생 이력을 최신순으로 자세히 보여줘

기대 동작:

- Turn 1 결과의 `LOT_ID=T1234567GEN1`을 상속합니다.
- Turn 2는 `followup_expand_source` 성격으로 `hold_history`만 신규 조회하고, `required_params.LOT_ID=T1234567GEN1`을 전달합니다.
- `lot_status`를 다시 전체 조회하지 않습니다.
- 결과는 2행이며 최신순입니다.
  1. `2026-07-01 08:00:00`, `H001`, `검증용 HOLD 이력`
  2. `2026-06-30 18:00:00`, `H000`, `검증용 이전 HOLD 이력`
- 최소 결과 컬럼은 `LOT_ID`, `HOLD_TM`, `HOLD_CD`, `HOLD_DESC`입니다.

## 4. 공통 합격 기준

- 질문 14~21은 `analysis.status=ok`이고 위 기대 row count와 핵심값을 만족해야 합니다.
- 질문 22~23도 오류가 아니라 정상 0건 분석이어야 합니다. `data.rows=[]`를 유지하고 존재하지 않는 값을 만들어내지 않습니다.
- 최종 API의 `data_mode`는 dummy 검증에서 `dummy`여야 합니다.
- 최종 row의 단일 소유 위치는 `data.rows`이며 answer section이나 trace에 전체 row를 중복하지 않습니다.
- 계산 과정에서 원천 dataset의 다른 제품·날짜·공정 제어행이 섞이지 않아야 합니다.
- 질문 24~25는 단일-turn validator 결과에 포함하지 않고 별도 2-turn 결과로 보고합니다.
