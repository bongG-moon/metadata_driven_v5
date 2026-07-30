# Data Analysis Flow v5 대표 질문과 기대 결과

갱신일: 2026-07-30
결정적 검증 기준일: `20260701`
기본 조회 모드: `04A.retrieval_mode=dummy`

## 구성 원칙

- 대표 질문은 총 30개다.
- 기존 질문 1~10은 회귀 연속성을 위해 그대로 유지한다.
- 11~30은 2026-07-29에 개선한 source별 필터 분리, 숫자 비교, 공정 범위, 공정 그룹 인식, 빈 값, 제품 비교, UPH 비가산 지표, 현재 HOLD, 제품별 장비 매칭을 검증한다.
- 실행 계약과 상세 기대값의 기준은 `tools/validate_representative_questions.py`다.
- 멀티턴 질문은 별도의 세션 기반 validator로 검증하며 이 30개 단일 실행 결과에 섞지 않는다.
- `hold_history`는 `LOT_ID`가 필수이므로 독립 질문에서 제외하고, 선행 LOT 결과를 사용하는 멀티턴 검증에서만 확인한다.

## 기존 대표 질문 1~10

1. `오늘 투입된 제품중 MCP NO가 L-267로 시작하는 제품의 INPUT 수량 알려줘`
2. `어제 DA공정 차수별 생산량 알려줘`
3. `어제 Mobile제품의 PKG OUT실적을 제품별로 알려줘`
4. `HBM제품의 WB공정에서 오늘 아침재공 제품별로 알려줘`
5. `6/27일 W/B공정에서 세부 공정별 생산실적과 아침재공 수량 알려줘`
6. `HBM제품 FCB공정에서 오늘 아침재공 제품별로 알려줘`
7. `6월 30일 FCB/H 공정 실적이 있는 Device 알려줘`
8. `RG 32G DDR4 FBGA 96 DDP 제품 BG공정에서 생산량과 재공수량 알려줘`
9. `FCB 공정에서 SP 16G DDR5 2ND X4 78 FCBGA SDP 제품의 전일 생산량 알려줘`
10. `6/24일 투입 실적 대비 D/S1, DA1공정에서 WIP 많은 제품 알려줘`

## 개선 대표 질문 11~30

| No. | 질문 요약 | 핵심 기대 결과 | 검증 목적 |
| ---: | --- | --- | --- |
| 11 | INPUT 실적은 있으나 DA WIP 없는 제품 | 6행, `INPUT_QTY`, `DA_WIP=0` | production은 INPUT, WIP는 DA로 source별 필터 분리 |
| 12 | FCB 생산과 W/B2 WIP 제품별 비교 | 4행, `FCB_PRODUCTION`, `WB2_WIP` | FCB 그룹과 단일 W/B2 조건을 서로 섞지 않음 |
| 13 | WB IN TAT 10시간 이상 LOT | 2행, 12.5시간과 11시간 LOT 포함, 8시간 LOT 제외 | `ge` 숫자 비교와 null-safe 실행 |
| 14 | D/S1~D/A4 범위 HOLD LOT | 2행, D/S1과 D/A5 HOLD 포함 | `OPER_SEQ` 숫자 범위 적용 후 HOLD 필터 실행 |
| 15 | D/A1~W/B6 공정별 생산량 | 13행 | 양 끝을 포함한 ordered range와 순서 정렬 |
| 16 | `DA, WB공정` HOLD LOT | 2행 | 공유 공정 접미사와 두 공정 그룹 확장 |
| 17 | `WB & DA 공정` HOLD LOT | 2행 | `&`로 연결된 그룹+그룹 인식 |
| 18 | `D/S1&D/A 공정` HOLD LOT | 2행 | 단일 공정과 공정 그룹의 결합 |
| 19 | FCB1, FCB2, FCB/H 실적 | 공정별 3행 | 명시 공정 나열 누락 방지 |
| 20 | D/A1, D/A2 실적 | 공정별 2행 | 복수 세부 공정 중 두 번째 값 누락 방지 |
| 21 | W/BM 제품별 생산량과 빈 값 | 4행, 빈 제품 차원 유지, null 생산량 0 | W/BM 단일 공정 및 blank/null 표시 |
| 22 | 같은 기준 키에서 다른 제품 속성 찾기 | 4행, MODE·PKG1·LEAD 변형 포함, MCP 대조군 제외 | 비교 기준과 비교 컬럼 분리 |
| 23 | FCB2 제품별 UPH | Recipe 상세 2행, 140.0과 173.4 | UPH 합산 금지와 기본 상세 컬럼 유지 |
| 24 | L-217 WB 차수·장비 기종별 UPH | W/B1 123.4, W/B2 97.5 | MCP prefix token helper와 평균 집계 |
| 25 | F315 L-116 WB 차수별 UPH | W/B1 평균 112.0, F316·L-117 대조군 제외 | LEAD와 MCP prefix token의 AND 조합 |
| 26 | D/A1 장비 모델·Recipe·공정·UPH | 1행, `EQM-HBM`, `RCP-002`, 88.2 | UPH 기본 표시 컬럼 |
| 27 | D/A1 할당 장비의 모델·Recipe | 1행, `EQP002`, `EQM-HBM`, `RCP-002` | UPH 미요청 시 equipment_assign만 사용 |
| 28 | WB 현재 HOLD LOT와 사유 | 1행, `T1234567GEN1` | 현재 HOLD 조건과 사유 표시 |
| 29 | 현재 HOLD LOT 상세 수량·TAT | 3행 | UNIT/Wafer 의미 매핑과 TAT 표시 |
| 30 | DA 상위 3개 제품별 장비 대수·LIST | 3행, blank MCP 제품 장비 2대 | 제품 행 단위 매칭, blank/null 정규화, 제품별 집계 |

## 대표 실행

```powershell
.\.venv\Scripts\python.exe tools\validate_representative_questions.py --output validation_outputs\representative_questions_dummy_fixture_20260730.json
```

합격 기준:

- `30/30 passed`
- 모든 결과의 `analysis.status=ok`
- Dummy 실행의 `data_mode=dummy`
- 질문별 dataset, source별 filter, 결과 컬럼, 기대 행과 대조군 제외 조건을 모두 만족
- Flow 원본과 export/import-ready JSON source가 동기화되어야 함

## 별도 멀티턴 검증

후속 질문은 [DATA_ANALYSIS_CURRENT_VALIDATION_QUESTION_SET_20260729.md](DATA_ANALYSIS_CURRENT_VALIDATION_QUESTION_SET_20260729.md)의 MT-1~MT-5를 동일 `session_id`로 실행한다. 대표 30문항이 통과해도 세션 저장, 이전 결과 참조, 조건 상속과 독립 질문 전환은 별도로 확인해야 한다. 특히 HOLD history는 MT-2처럼 선행 결과의 `LOT_ID`가 전달되는 경로로 검증한다.
