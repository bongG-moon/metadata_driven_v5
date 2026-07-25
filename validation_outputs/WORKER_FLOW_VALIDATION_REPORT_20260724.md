# 작업자 질문 실제 Flow 검증 보고서

- 검증일: 2026-07-24
- 기준 저장소: `C:\Users\qkekt\Desktop\metadata_driven_v5`
- 기준일: `20260701`
- 메타데이터: MongoDB `datagov`의 v4 호환 컬렉션
- 데이터 조회: v5 Dummy Retriever
- 모델: `.env`에 설정된 `gemini-2.5-flash`
- 실행 경로: Metadata 후보 → 의도 분석 → 신뢰 카탈로그 보강 → Dummy 조회 → pandas 생성/실행 → 답변 생성

## 1. 환경 확인

| 항목 | 결과 |
| --- | --- |
| MongoDB 연결 | 정상 |
| 도메인 메타데이터 | 71건 |
| 테이블 카탈로그 | 9건 |
| 메인 필터 | 17건 |
| 의도 프롬프트 | 실제 v5 파일 사용 |
| pandas 프롬프트/1회 복구 | 실제 v5 파일 사용 |
| 답변 프롬프트 | 실제 v5 파일 사용 |

## 2. Data Analysis 검증 결과

총 12건을 실행했다. 전체 파이프라인 평균 시간은 56.37초, 최소 20.91초, 최대 90.44초였다.

| 구분 | 질문 | 판정 | 확인 결과 |
| --- | --- | --- | --- |
| 공정 그룹 | 오늘 B/G 공정의 생산량과 현재 재공을 같이 알려줘. | 통과 | `BG → B/G1, B/G2`로 확장하고 `production_today`, `wip_today` 양쪽에 `DATE=20260701`을 적용했다. |
| 공정 별칭 | 오늘 WBM공정과 W/B공정의 생산량을 공정별로 비교해줘. | 조건부 통과 | 실제 계획은 `W/BM`과 `W/B1~W/B6`을 올바르게 조회해 7행을 반환했다. 다만 검증기가 `WBM` 내부의 `BM`을 별도 그룹으로 오인해 오류로 표시했다. |
| 제품/복합 공정 | 오늘 A-578 제품의 DA공정과 WB공정에서 각각 생산량과 재공 수량을 알려줘. | 계획 통과 | 데이터셋과 공정 그룹 계획은 정상이다. Dummy에 `A-578` 제품 행이 없어 결과는 0행이다. |
| 제품 토큰 | SP 14G 2ND X4 FC78 제품의 현재 재공을 알려줘. | 계획 통과 | `match_product_tokens`를 선택하고 `X4 → ORG=4`, `FC78 → PKG1=FCBGA + LEAD=78` 계약을 사용했다. Dummy에는 `14G` 일치 행이 없어 집계값은 0이다. |
| 장비 목록 | 현재 DA공정에서 L-218K8H 제품을 작업하는 장비를 알려줘. | 계획 통과 | `lot_status`가 아닌 `equipment_assign`을 선택했다. Dummy의 L-218K8H 장비 행은 D/S1에 있어 DA 필터 결과는 0행이다. |
| 장비/UPH | 현재 D/A1 공정에 배정된 장비와 해당 모델의 UPH를 함께 보여줘. | 통과 | `equipment_assign + eqp_uph`를 결합하고 `EQUIP_ID, EQUIP_MODEL, OPER_NAME, RECIPE_ID, UPH`를 반환했다. |
| 제품/장비 결합 | 현재 DA공정에서 재공이 가장 많은 제품 10개와 각 제품에 할당된 장비를 같이 보여줘. | 통과 | 도메인 제품 키를 사용하고 `DEVICE`를 group by에서 제외했다. 빈 `MCP_NO` 제품도 유지했으며 장비 ID가 결합됐다. |
| 공정 구간 | 오늘 D/A1부터 W/B6까지 공정 구간의 생산량과 재공을 공정 순서대로 보여줘. | 통과 | `filter_ordered_range`를 사용해 양 끝을 포함한 13개 공정을 `OPER_SEQ` 순서로 반환했다. |
| 데이터셋별 날짜 | 어제 MOBILE 제품의 생산량과 오늘 현재 재공을 각각 알려줘. | 통과 | 생산량은 `production DATE=20260630`, 재공은 `wip_today DATE=20260701`로 분리했다. |
| Shift | 오늘 A조에서 DA공정 생산량이 가장 많은 제품 3개를 알려줘. | 실패 | MongoDB에는 `status_terms:SHIFT_A`가 `SHIFT=1`로 정상 등록돼 있지만 후보에 포함되지 않아 `SHIFT=A` 필터를 생성했고 결과가 0행이 됐다. |
| 계획/달성률 | 7월 1일 제품별 INPUT 계획 대비 실제 INPUT 실적과 달성률을 알려줘. | 통과 | `production + target`을 결합하고 제품 키, INPUT 계획, 실제 INPUT 실적, 달성률 8행을 반환했다. |
| HOLD 이력 | 현재 HOLD 상태인 LOT을 보여주고 각 LOT의 최근 HOLD 이력도 같이 알려줘. | 통과(표시 보완 필요) | `lot_status + hold_history`를 결합해 1행을 반환했다. 조인 결과에 `OPER_NAME_y`가 남아 최종 표시 컬럼 정리가 필요하다. |

### Data Analysis 핵심 결함

1. `A조`의 canonical 조건이 후보 단계에서 누락된다.
   - 저장된 도메인: `status_terms:SHIFT_A`, `condition={"SHIFT": "1"}`
   - 실제 계획: `SHIFT={"operator":"eq","value":"A"}`
   - 필요한 개선 위치: `01d_metadata_candidates_builder`의 짧은 alias/상태 조건 점수와 의도 프롬프트의 canonical condition 우선 적용

2. 결과가 0행이어도 실행 오류가 없으면 검증 상태가 `ok`가 된다.
   - 제품/공정 조합에 따라 정상적인 0행일 수 있으므로 무조건 오류 처리할 수는 없다.
   - 다만 검증 질문에 기대 fixture가 있는 경우에는 별도의 `expected_non_empty` 검사를 두는 편이 안전하다.

3. HOLD 결합 결과에 suffix 컬럼이 남는다.
   - `OPER_NAME`과 `OPER_NAME_y` 중 최종 의미에 맞는 하나를 선택하도록 pandas 출력 계약을 보강할 필요가 있다.

## 3. Metadata QA 검증 결과

실제 MongoDB snapshot과 Metadata QA의 결정론적 context/normalizer/message 경로로 6건을 실행했다.

| 질문 | 실행 상태 | 품질 판정 | 확인 결과 |
| --- | --- | --- | --- |
| 현재 조회 가능한 데이터셋 목록을 알려줘. | ok | 통과 | 등록된 데이터셋 9개만 표로 반환하고 Oracle 8개, Goodocs 1개를 설명했다. |
| 현재 등록된 공정 그룹 도메인 목록을 알려줘. | ok | 실패 | `process_groups` 외 분석 레시피와 수량 용어까지 섞인 39건을 반환했다. |
| BG 공정 그룹에는 어떤 세부 공정이 포함돼? | ok | 실패 | BG 외 다른 공정 그룹과 레시피까지 37건을 반환했고 핵심 `processes=B/G1,B/G2`를 표에 표시하지 못했다. |
| 제품 집계는 어떤 컬럼을 기준으로 해? | ok | 통과 | 표준 제품 키 2건과 실제 컬럼 목록을 반환했다. |
| 장비 목록 질문에서 equipment_assign과 lot_status는 어떻게 구분해? | ok | 실패 | `equipment_assign`만 후보에 포함되고 `lot_status`가 빠져 비교 설명이 되지 않았다. |
| eqp_uph 데이터셋에서 기본으로 보여주는 컬럼을 알려줘. | ok | 실패 | 전체 스키마는 표시했지만 등록된 기본 표시 컬럼 계약을 별도로 설명하지 못했다. |

### Metadata QA 핵심 결함

1. 공정 그룹 inventory/detail 질문은 `section=process_groups`로 먼저 제한해야 한다.
2. 특정 그룹 질문에서는 canonical key가 일치하는 항목을 최우선 단건으로 선택하고 `payload.processes`를 표시해야 한다.
3. 두 데이터셋 비교 질문은 질문에 명시된 양쪽 dataset key를 모두 강제 후보로 유지해야 한다.
4. 테이블 상세 projection에 `default_detail_columns` 등 사용자에게 설명할 등록 기준을 포함해야 한다.
5. 현재 `status=ok`는 컴포넌트 실행 성공을 뜻할 뿐 답변 적합성을 보장하지 않으므로 질문 유형별 품질 assertion이 필요하다.

## 4. Flow/JSON 구조 검증

선택 테스트 329개 중 328개가 통과했고 1개가 실패했다.

실패 원인은 API Router의 `ApiCaller-*` 5개 노드에 포함된 코드가 현재 Python 원본과 정확히 같지 않기 때문이다.

- Python 원본: `langflow_components/route_flow/01_flow_api_message_caller.py`
- JSON 내장 코드: 세션 ID 해석에서 GaiA metadata 우선 처리 로직이 빠진 이전 코드
- 영향: 새로 import하는 Router JSON은 Python 원본에 반영된 최신 세션 전달 개선을 포함하지 않는다.
- 권장 조치: Router Flow를 다시 생성한 뒤 개별 JSON, 전체 bundle, manifest, ZIP을 동기화하고 source audit를 재실행한다.

## 5. 실행 로그

- `worker_q01_bg_production_wip.json`
- `worker_batch_process_product.json`
- `worker_q_equipment_assign.json`
- `worker_q_equipment_uph.json`
- `worker_q_wip_top_equipment.json`
- `worker_q_oper_seq_range.json`
- `worker_q_split_dates.json`
- `worker_q_shift_a.json`
- `worker_q_plan_achievement.json`
- `worker_q_hold_history.json`
- `worker_metadata_qa_validation.json`

모든 JSON은 `validation_outputs` 폴더에 저장되어 있다.

## 6. 최종 판정

- 공정 그룹 확장, 데이터셋별 날짜, 제품 grain, 장비/UPH 결합, OPER_SEQ 구간, 계획/실적 달성률은 정상 동작한다.
- 즉시 개선이 필요한 항목은 `A조 → SHIFT=1` canonical 변환과 Metadata QA의 공정 그룹/데이터셋 비교 후보 제한이다.
- Router JSON은 Python 원본보다 오래된 코드가 들어 있으므로 재생성이 필요하다.
- 실제 Langflow 런타임 import/API 호출은 이번 검증 범위에 포함하지 않았다. 이번 결과는 실제 Flow Python 원본과 프롬프트를 같은 순서로 실행한 독립 검증 결과다.
