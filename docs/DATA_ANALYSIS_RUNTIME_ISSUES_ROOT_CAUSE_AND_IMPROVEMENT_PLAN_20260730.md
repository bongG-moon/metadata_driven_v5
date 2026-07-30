# Data Analysis 실제 환경 문제 원인 분석 및 개선 계획

- 작성일: 2026-07-30
- 기준 브랜치: `main`
- 기준 런타임: Langflow 1.9.2 / Langflow Base 0.9.2 / LFX 0.4.2 / Python 3.12

## 1. 결론

이번 세 증상은 각각의 pandas 코드 한 줄만 고치면 끝나는 문제가 아니다. 공통적으로 LLM이 만든 계획과 pandas 코드 뒤에 다음 결정적 계약이 부족해서 발생한다.

1. 질문의 각 지표가 어느 dataset, 날짜, source alias, 물리 컬럼에서 왔는지 검증하는 **metric-source lineage 계약**
2. 실행용 물리 컬럼과 최종 표시용 표준 컬럼을 분리하고, 허용된 최종 컬럼만 내보내는 **strict output schema 계약**
3. 후속 질문에서 이전 결과와 새 source의 실제 컬럼을 한 번만 해석해 결합하는 **resolved reference join 계약**

또한 독립 실행이 불가능한 HOLD history 질문이 dummy 경로에서는 통과하고 있었다. 이는 질문의 문제가 아니라 dummy 검증이 실제 catalog의 필수 파라미터 계약을 재현하지 않은 false positive다.

이 문서는 원인과 구현 계획을 확정한 뒤 해당 계획의 구현·검증 결과까지 기록한다. 잘못된 HOLD 단일턴 질문은 활성 검증 세트에서 제거했고, 나머지 세 건은 2026-07-30에 runtime 계약과 결정적 실행 경로로 개선했다.

## 2. 문제별 판정 요약

| 문제 | 기대 동작 | 실제 증상 | 직접 원인 | 구조적 원인 |
| --- | --- | --- | --- | --- |
| 6/27 W/B 생산실적 + 아침재공 | `production/20260627`과 `wip/20260626`을 각각 집계 후 결합 | 생산 source만으로 답하고 WIP를 생산량에서 복사 | WIP job 누락·오선택 후 `WIP_QTY = PRODUCTION_QTY` 생성 | 날짜 의미와 metric provenance를 강제하지 않음 |
| INPUT 실적 있음 + D/A WIP 없음 | INPUT metric 1개, D/A WIP metric 1개 | `DA_WIP_SUM`과 generic `WIP`가 같은 값으로 중복 표시 | LLM이 계약 외 alias 컬럼을 추가하고 adapter가 모두 표시 | 의미 기준 중복 제거와 허용 컬럼 검사가 없음 |
| DA 상위 3개 → 장비 후속 질문 | 이전 제품 3행을 보존하고 장비 대수·LIST만 결합 | `DENSITY` 누락 output contract 오류 | `DEN`·`DENSITY`를 source별 수동 map으로 다시 해석 | canonical/physical vocabulary와 follow-up join이 결정적으로 고정되지 않음 |
| 현재 HOLD LOT별 최신 HOLD 이력 | 선행 LOT_ID가 있어야 history 조회 가능 | 독립 질문에는 필수 `LOT_ID`가 없음 | `hold_history`의 필수 파라미터를 만들 수 없음 | dummy validator가 실제 필수 파라미터 계약을 검사하지 않음 |

## 3. 6/27 W/B 생산실적과 아침재공

### 3.1 올바른 실행 계약

| 출력 지표 | dataset | 조회 DATE | source column | 공정 조건 | 집계 |
| --- | --- | --- | --- | --- | --- |
| `PRODUCTION_QTY` | `production` | `20260627` | `PRODUCTION` | W/B1~W/B6 | `OPER_NAME`별 sum |
| `WIP_QTY` | `wip` | `20260626` | `WIP` | W/B1~W/B6 | `OPER_NAME`별 sum |

두 집계 결과는 `OPER_NAME`으로 outer merge하고, **정상 조회가 완료된 source가 0행인 경우에만** 해당 지표를 0으로 채워야 한다. source 누락·조회 실패·잘못된 dataset 선택은 0이나 다른 지표로 대체하지 말고 오류로 중단해야 한다.

### 3.2 확인된 원인

1. **질문 속 날짜의 전일이 구조화되지 않는다.**
   - `01e_followup_hint_builder.py`는 질문의 `6/27`을 해석하지만 그 날짜의 D-1을 별도 값으로 만들지 않는다.
   - `02_intent_variables_builder.py`의 `request_context.previous_date`는 질문의 6/27이 아니라 실행 기준일의 전일이다.
   - `specialized_prompt_input_example_ko.md`는 BOH에 이 전역 `previous_date`를 사용하도록 안내한다. 저장된 live 검증에서도 이 질문의 WIP 날짜가 `20260630`으로 선택된 이력이 있으며, 올바른 날짜는 `20260626`이다.

2. **BOH 규칙이 실행 가능한 구조 데이터가 아니다.**
   - `domain_knowledge.txt`에는 특정일 BOH가 전일 `wip` 이력이라는 규칙이 명시돼 있다.
   - export된 domain item의 상세 시간 규칙은 주로 `registration_trace.raw_text`에 남아 있지만, metadata candidate builder는 prompt 전달 전에 이 필드를 제거한다.
   - alias는 `아침 재공`인데 질문의 `아침재공`처럼 공백이 없는 표현은 현재 token 일치에서 약해질 수 있다.

3. **요청한 지표별 source 완전성을 검사하지 않는다.**
   - intent prompt에는 서로 다른 dataset의 지표를 별도 job으로 만들라는 지침이 있다.
   - 하지만 normalizer, hydrator, retrieval validator와 execution gate는 LLM이 이미 만든 job을 검사할 뿐, 질문이 요구한 생산량과 BOH WIP에 대응하는 두 job이 모두 존재하는지 역으로 대조하지 않는다.
   - 계획에서 WIP job 자체가 빠지면 gate는 “빠진 계획”을 실패로 인식하지 못한다.

4. **aggregate 결과는 컬럼 이름만 맞으면 출처를 검증하지 않는다.**
   - pandas prompt는 없는 값을 발명하지 말라고 안내하지만 soft rule이다.
   - executor는 결과 컬럼의 존재 여부를 주로 검사하고, `WIP_QTY`가 실제 `wip_data.WIP`에서 계산됐는지 확인하지 않는다.
   - 따라서 `grouped["WIP_QTY"] = grouped["PRODUCTION_QTY"]`처럼 이름만 맞춘 가짜 지표가 계약을 통과할 수 있다.

5. **현재 대표 dummy 검증은 이 실패를 가릴 수 있다.**
   - `tools/validate_representative_questions.py`의 기본 경로는 이 질문에 정답 retrieval job과 pandas 코드를 직접 주입한다.
   - 따라서 30/30 static 통과는 LLM이 dataset·날짜·코드를 올바르게 생성했다는 의미가 아니다.

### 3.3 개선 계획

- 질문에서 찾은 날짜 mention을 `requested_date`로 구조화하고 Domain의 일수 offset으로 실제 `query_date`를 계산한다.
- BOH Domain payload 자체에 다음 실행 규칙을 저장하고 candidate prompt에도 원문 그대로 보존한다. Candidate Builder나 Intent Normalizer가 BOH key·alias를 보고 계약을 추가하지 않는다.

```json
{
  "business_timepoint": "BOH",
  "dataset_family": "wip_history",
  "dataset_key": "wip",
  "date_param": "DATE",
  "requested_date_offset_days": -1,
  "disallowed_dataset_keys": ["wip_today"],
  "inherit_filters": true
}
```

- 공백·구두점 제거 후 alias를 비교해 `아침재공`과 `아침 재공`을 같은 표현으로 취급한다.
- Intent Normalizer의 범용 temporal contract 처리기는 선택된 Domain의 `dataset_key`, `date_param`, `requested_date_offset_days`, `disallowed_dataset_keys`만 해석하며 특정 업무 키워드를 검사하지 않는다.
- intent/output contract에 다음 형태의 metric binding을 추가한다.

```json
[
  {
    "output_column": "PRODUCTION_QTY",
    "source_alias": "production_data",
    "dataset_key": "production",
    "source_column": "PRODUCTION",
    "query_date": "20260627",
    "aggregation": "sum"
  },
  {
    "output_column": "WIP_QTY",
    "source_alias": "wip_data",
    "dataset_key": "wip",
    "source_column": "WIP",
    "query_date": "20260626",
    "aggregation": "sum"
  }
]
```

- retrieval 전 requested metric과 planned job을 대조하고, alias 중복과 planned/result source set 불일치를 fail-closed로 처리한다.
- 서로 다른 지표의 직접 복사는 명시적으로 허용된 계산식이 없으면 executor에서 거부한다.
- 정상 조회 0행과 source 누락·실패를 구분한다. 전자만 0-fill을 허용한다.
- 단순 source별 집계와 merge는 가능하면 LLM 자유 코드가 아니라 lineage 기반 deterministic operation으로 생성한다.

## 4. D/A WIP 중복 컬럼

### 4.1 확인된 원인

제공된 코드는 `DA_WIP_SUM`을 만든 뒤 다시 generic `WIP=0`을 추가하고 두 컬럼을 모두 선택한다. 현재 구조에서는 이 코드가 다음 이유로 살아남는다.

- `output_contract.required_columns`와 `metric_columns`는 문자열 이름 기준으로만 합쳐진다. `DA_WIP_SUM`과 `WIP`가 같은 source·조건·지표를 나타내는지는 알 수 없다.
- executor는 계약에 없는 extra metric이나 의미상 같은 alias 컬럼을 거부하지 않는다.
- answer response builder와 message adapter는 결과 DataFrame의 전체 컬럼을 기본 표시 대상으로 사용한다.
- prompt에는 `column_labels`를 표시용으로만 사용하라는 안내가 있지만, 최종 컬럼 집합은 결정적으로 제한되지 않는다.

따라서 이 문제는 단순 rename 중복이 아니라 **동일 metric binding에 여러 출력 이름을 허용하는 계약 공백**이다.

### 4.2 개선 계획

- metric을 `source_alias + source_column + filter scope + time scope + aggregation`으로 식별하고 binding당 canonical output column을 하나만 허용한다.
- 계약을 다음 두 층으로 분리한다.
  - `physical_execution_columns`: source 처리와 join에 사용할 실제 컬럼
  - `result_columns`: 최종 응답에 허용된 canonical 컬럼과 순서
- executor에 strict allowed-column 검사를 추가한다.
  - 같은 binding의 두 컬럼 값이 같으면 계약 컬럼 하나만 남기고 validation mode에서는 중복 오류를 기록한다.
  - 값이 다르면 자동 선택하지 않고 `semantic_output_collision`으로 중단한다.
  - 서로 다른 source·공정·시점의 WIP처럼 실제 의미가 다른 지표는 별도 binding으로 유지한다.
- answer builder는 DataFrame 전체가 아니라 계약의 `result_columns`만 렌더링한다.
- 표시 label 충돌도 별도로 검사한다.

이 질문의 최종 metric은 `INPUT_QTY`와 `DA_WIP_QTY` 두 개만 허용하며 generic `WIP`는 없어야 한다.

## 5. DA 상위 제품의 장비 후속 질문

### 5.1 확인된 원인

metadata의 표준 제품 키는 `DEN`, `PKG_TYPE1`, `PKG_TYPE2`지만 실제 source와 이전 결과는 `DENSITY`, `PKG1`, `PKG2`를 사용할 수 있다.

현재 normalizer는 일부 aggregate 출력 grain을 source 물리 이름으로 바꾸면서도 follow-up match column은 canonical 이름으로 유지한다. executor의 자동 row-match는 source와 이전 결과에서 후보 컬럼을 찾지만, 이후의 “이전 결과 3행을 left로 유지하고 장비 집계를 붙이는 join과 최종 projection”은 LLM 코드에 남아 있다. 그래서 LLM이 `prev_map`과 `ea_map`을 다시 만들고, merge suffix 또는 잘못된 최종 컬럼 선택으로 `DENSITY`를 잃을 수 있다.

현재 main의 executor는 hydrated job에 `DEN -> DENSITY` mapping이 있으면 alias 동등성을 인식한다. 반대로 mapping이 없으면 사용자가 제시한 `DENSITY` 누락 오류가 재현된다. repository catalog에는 mapping이 선언돼 있으므로 실제 환경에서는 다음을 구분해야 한다.

- failing payload에 mapping이 없음: MongoDB Table Catalog 또는 hydrator 배포 상태 문제
- mapping이 있는데 같은 오류 발생: 실제 executor node가 현재 main보다 오래된 배포일 가능성
- mapping과 node가 최신인데도 재발: LLM 수동 join/projection의 비결정성

저장된 live 검증도 같은 후속 질문을 여러 번 재실행한 뒤에야 통과했다. 한 번의 성공은 구조적 안정성을 보장하지 않는다.

### 5.2 개선 계획

- `canonical output grain`과 `physical execution grain`을 명시적으로 분리한다.
- hydrator가 확정한 양방향 alias group을 payload에 보존한다.
  - `DEN <-> DENSITY`
  - `PKG_TYPE1 <-> PKG1`
  - `PKG_TYPE2 <-> PKG2`
- executor의 row-match가 실제로 해결한 `resolved_columns`를 후속 join의 유일한 key mapping으로 재사용한다.
- normalizer가 `resolved_reference_join_plan`을 생성한다.
  1. `previous_result`를 left로 유지
  2. equipment source를 resolved key로 집계
  3. 오른쪽에서는 임시 key와 `EQUIP_COUNT`, `EQUIP_LIST`만 선택
  4. left merge
  5. 이전 결과의 컬럼명과 순서를 보존
  6. 장비가 없는 행은 count 0, list 빈 문자열
- LLM이 `prev_map`·`ea_map`을 새로 작성하지 못하게 하고, deterministic join operator가 결합을 수행한다.
- 최종 canonical 변환은 executor 경계에서 한 번만 수행하며 `_x`, `_y` suffix를 허용하지 않는다.

## 6. HOLD history 단일턴 질문 제거

제거 대상:

> 현재 HOLD 중인 LOT별 가장 최근 HOLD 코드와 상세 사유를 알려줘

`data_catalog.txt`에서 `hold_history`는 `LOT_ID`가 필수이고 쿼리도 `WHERE LOT_ID IN ({LOT_ID})`다. 이 값은 같은 세션의 선행 `previous_result`에서 전달하도록 정의돼 있다. 독립 질문에는 LOT_ID도 선행 결과도 없으므로 실제 retriever는 `missing_required_params`로 중단하는 것이 정상이다.

기존 dummy 대표 검증은 실제 catalog의 `hold_history.required_params=LOT_ID`를 재현하지 않고 전체 이력 데이터를 반환해 false positive를 만들었다.

처리 원칙:

- 독립 단일턴 질문은 자동 대표 질문과 작업자 수동 단일턴 목록에서 제거한다.
- 다음 유효한 멀티턴 검증은 유지한다.
  1. `W/B공정 현재 HOLD LOT와 HOLD사유 알려줘`
  2. `HOLD 시간이 가장 오래된 LOT의 이력을 보여줘`
- 날짜가 포함된 2026-07-29 과거 validation output과 구현 보고서는 당시 실행 증거이므로 소급 수정하지 않는다.
- 후속 개선에서 dummy catalog와 dummy retriever도 실제 required-param 계약을 적용해 같은 false positive를 차단한다.

## 7. 구현 우선순위

### P0. 즉시 차단과 운영 trace 확보

- source 누락·실패 시 다른 metric 복사와 0 대체를 금지한다.
- output contract에 없는 extra semantic metric을 validation mode에서 실패시킨다.
- source alias 중복과 source merge의 silent overwrite를 금지한다.
- 실제 실패 payload에서 다음을 함께 수집한다.
  - normalized intent plan과 metric bindings
  - hydrated `filter_mappings`, `standard_column_aliases`
  - source별 dataset, params, status, row count, schema
  - generated pandas code와 repair 사용 여부
  - import된 Flow의 `last_tested_version`, 각 node `lf_version`, component source hash

### P1. 구조 계약 강화

- BOH temporal semantics와 날짜 D-1 파생
- metric-source lineage와 source completeness gate
- physical/canonical output schema 분리
- strict result columns와 semantic duplicate 검사
- resolved previous-result join plan

### P2. 결정적 실행으로 전환

- source별 filter → groupby → aggregate → join 패턴을 deterministic operation으로 처리
- 이전 결과 enrich와 compare-presence 분석을 deterministic operator로 처리
- LLM은 intent와 설명을 만들고, 검증된 operation spec만 실행하도록 범위를 축소

### P3. 검증·배포 게이트

- static fixture 외에 실제 intent LLM → hydration → retrieval → pandas code → executor 경로를 차단성 회귀 테스트로 운영
- 동일 질문을 반복 실행해 결과 스키마와 repair 사용 여부의 안정성을 확인
- Python source와 Flow JSON을 함께 수정·재생성
- `tools/validate_flow_component_sources.py`와 정확한 Langflow 1.9.2 node template parse를 통과
- 실제 운영 Flow를 재import한 뒤 live source smoke와 2-turn 검증 실행

## 8. 필수 합격 기준

| 시나리오 | 합격 기준 |
| --- | --- |
| 6/27 W/B 생산 + 아침재공 | job 2개, `production/20260627`, `wip/20260626`, 서로 다른 source metric lineage, 생산값 복사 없음 |
| WIP source 0행 | source status가 성공일 때만 WIP 0 허용 |
| WIP source 누락·실패 | 결과 생성 없이 명시적 contract error |
| INPUT 있음 + DA WIP 없음 | 제품 grain + `INPUT_QTY` + `DA_WIP_QTY`만 출력, generic `WIP` 없음 |
| DA 상위 3개 → 장비 | 이전 3행 모두 보존, `DENSITY` 누락 없음, suffix 없음, 제품별 `EQUIP_COUNT`·`EQUIP_LIST` |
| 장비 없는 이전 제품 | 행 유지, `EQUIP_COUNT=0`, `EQUIP_LIST=""` |
| 반복 안정성 | 같은 2-turn 질문을 최소 3회 연속 실행해 repair 없이 동일 컬럼 집합 |
| HOLD history | 독립 질문은 활성 세트에 없음, 선행 LOT_ID가 있는 멀티턴만 성공 |

## 9. 예상 수정 범위

실제 구현 시 최소한 다음 영역이 함께 변경돼야 한다.

- 날짜·후속 힌트: `01e_followup_hint_builder.py`, `02_intent_variables_builder.py`
- metadata candidate와 domain 저장 payload: `01d_metadata_candidates_builder.py` 및 Domain Metadata 저장 Flow
- intent/계약: `03_intent_prompt_template_ko.md`, `04_intent_plan_normalizer.py`
- catalog hydration·retrieval gate: `04a_trusted_retrieval_job_hydrator.py`, `06_retrieval_job_validator.py`, `13_source_retrieval_merger.py`, `14a_retrieval_execution_gate.py`
- pandas 입력·실행: `15_pandas_variables_builder.py`, `16_pandas_prompt_template_ko.md`, `17_pandas_code_executor.py`
- 최종 표: `20_answer_response_builder.py`, `21_answer_message_adapter.py`
- Python source와 Data Analysis 관련 Flow/export/import-ready JSON 동기화

실제 환경에서만 재현되는 부분은 먼저 배포 Flow와 현재 repository source hash를 대조한 뒤 구현한다. 이 확인 없이 현재 main 코드와 운영 instance가 동일하다고 가정하지 않는다.

## 10. 구현 및 검증 결과

### 10.1 구현 완료

- `01E`는 질문의 명시 날짜와 그 전일을 각각 `resolved_value`, `previous_value`로 구조화한다.
- metadata candidate는 `아침재공`과 `아침 재공`을 동일하게 매칭하고 BOH 시간 계약을 compact payload에 보존한다.
- `04` normalizer는 생산실적+아침재공 질문에서 `production/D`, `wip/D-1` job을 결정적으로 보장하고 `resolved_metric_merge_plan`과 source별 `metric_bindings`를 만든다.
- retrieval validator와 source merger는 중복 `source_alias`를 fail-closed로 처리한다.
- output contract는 같은 metric binding의 일반 alias를 제거하고 `result_columns`를 strict schema로 고정한다.
- `17` executor는 생산+BOH 병합과 이전 결과+장비 enrich를 LLM 자유 merge 코드 대신 내부 deterministic operator로 실행한다.
- 서로 다른 source binding의 metric 직접 복사는 AST 계약 오류로 차단한다.
- 후속 장비 조회는 `DEN/DENSITY`, `PKG_TYPE1/PKG1`, `PKG_TYPE2/PKG2`를 pandas 코드 생성 전에 해석하고 이전 결과를 left로 보존한다.
- 다음 턴 state에는 실제 grain `column_mappings`를 함께 보존한다.
- Python component 원본, standalone Flow JSON, 개별 import-ready JSON, 통합 JSON과 ZIP을 함께 재생성했다.

### 10.2 회귀 검증

| 검증 | 결과 |
| --- | --- |
| 신규 runtime 계약 회귀 | 7/7 통과 |
| Data Analysis 컴포넌트 전체 | 395/395 통과 |
| 저장소 비웹 pytest | 505/505 통과 |
| 대표 Dummy 질문 | 30/30 통과 |
| 대표 5번 production+BOH | `production/20260627`, `wip/20260626`, 공정별 독립 집계·outer merge 통과 |
| 후속 장비 enrich | 이전 3행 보존, 장비 없음 0/빈 목록, suffix·`DENSITY` 누락 없음 |

운영 Oracle/MongoDB 자격증명이 없는 로컬 환경이므로 실제 live source smoke는 배포 후 별도 수행해야 한다. 운영 Flow import 후에는 동일 2-turn 질문을 3회 반복해 `execution_mode=enrich_previous_result`, `llm_code_executed=false`, 동일 결과 컬럼 집합을 확인한다.
