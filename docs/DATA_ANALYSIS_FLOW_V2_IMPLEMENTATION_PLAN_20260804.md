# Data Analysis Flow V2 구현 계획

- 작성일: 2026-08-04
- 구현 대상: `data_analysis_flow_v2`
- 기준 런타임: Langflow 1.9.2 / langflow-base 0.9.2 / LFX 0.4.2 / Python 3.12
- 기준선: 현재 `data_analysis_flow_v5_standalone`
- 구현 상태: 완료. 기존 Flow를 유지한 별도 V2 Flow와 import-ready 산출물을 생성하고 회귀 검증한다.

## 1. 목적

기존 Data Analysis Flow는 의도 분석 이후에도 pandas 코드 생성 LLM과 답변 생성 LLM을 순차 호출한다. 단일 데이터셋의 필터·집계·정렬만 필요한 질문도 같은 경로를 사용하므로 응답 시간이 길고, 모델이 실제 source에 없는 컬럼이나 불필요한 결과 컬럼을 생성할 가능성이 있다.

V2의 목적은 다음과 같다.

1. 기존 `data_analysis_flow`와 export/import-ready JSON은 변경하지 않는다.
2. 단일 source에서 결정론적으로 실행 가능한 분석은 Fast Path로 처리한다.
3. Fast Path에서는 의도 분석 LLM만 사용하고 pandas 코드 생성 및 답변 생성 LLM은 호출하지 않는다.
4. 다중 source 결합, 사용자 정의 계산, 복잡한 후속 분석은 기존 LLM 기반 Complex Path를 유지한다.
5. 질문·공정·제품·컬럼명을 공통 컴포넌트에 하드코딩하지 않는다.
6. 모든 custom component는 export된 Flow 하나만으로 동작하는 standalone 조건을 유지한다.

## 2. 변경하지 않는 원칙

### 2.1 기존 Flow 격리

- `langflow_components/data_analysis_flow/` 원본은 V2 구현을 위해 변경하지 않는다.
- 기존 `tools/build_v5_data_analysis_flow.py`와 `flow_exports/data_analysis_flow_v5_standalone.json`을 변경하지 않는다.
- V2는 별도 builder, 별도 V2 전용 component, 별도 export로 생성한다.
- 변경되지 않는 기존 component source는 V2 builder가 읽어 새 JSON에 포함할 수 있다. 이는 build-time 재사용이며 runtime import가 아니다.
- 운영 전환은 기존 Flow를 덮어쓰지 않고 별도 Flow ID로 import하여 비교 검증한 뒤 결정한다.

### 2.2 Standalone

- custom component가 실행 중 sibling Python 파일을 import하지 않는다.
- Flow JSON에는 실행에 필요한 custom component source가 모두 포함되어야 한다.
- MongoDB URI, database, collection, timeout, 조회 제한, 모델 선택과 API key 같은 운영 설정은 기존 기준대로 노드 입력에서 확인하고 바꿀 수 있어야 한다.
- 공통 helper가 필요하면 builder 단계에서 source에 포함하거나 해당 standalone component 안에 순수 함수로 포함한다.
- Langflow 기본 Language Model source와 component index는 반드시 1.9.2 고정 자산을 사용한다.

### 2.3 메타데이터 주도 규칙

공통 노드에는 다음 값을 직접 작성하지 않는다.

- 공정명 또는 공정 그룹: `D/A`, `W/B`, `FCB` 등
- 제품명 또는 제품 속성: `HBM`, `POP`, `MOBILE` 등
- 특정 테이블의 물리 컬럼 alias: `DENSITY`, `PKG1`, `EQUIP_MODEL` 등
- 특정 metric의 날짜 offset 또는 단위 보정
- 질문 원문에만 반응하는 정규식 또는 문자열 예외

위 정보의 신뢰 원천은 다음과 같다.

| 정보 | 신뢰 원천 |
| --- | --- |
| 공정 그룹과 제품 조건 | 선택된 Domain metadata |
| 시간 의미와 날짜 offset | 선택된 Domain의 `temporal_semantics` |
| dataset과 source 설정 | Active Table Catalog |
| canonical/physical 컬럼 매핑 | 해당 테이블의 `filter_mappings` 및 표준 컬럼 계약 |
| metric source와 집계 | `output_contract.metric_bindings` |
| 사용자 표시명 | `output_contract.column_labels` |
| Function Case | 메타데이터가 명시적으로 선택한 function case 계약 |

매핑이 없거나 여러 실제 컬럼으로 모호하게 해석되면 임의 fallback을 만들지 않고 계약 오류로 차단한다.

## 3. 목표 아키텍처

```text
요청/세션/메타데이터 후보
        ↓
Intent Prompt → Intent LLM 1회
        ↓
Intent 정규화 → Trusted Catalog Hydration → Retrieval 검증
        ↓
Source 조회 → Merge → Retrieval Gate → 표준 컬럼 정규화
        ↓
Simple Analysis Contract Resolver
        ↓
┌──────────────────────────────────────────────────────┐
│ Hybrid Analysis Executor                             │
│                                                      │
│ Fast eligible                                        │
│   → 고정 Filter/집계/정렬 실행                       │
│   → pandas 생성 LLM 0회                              │
│                                                      │
│ Complex required                                     │
│   → pandas prompt 렌더링                             │
│   → pandas 생성 LLM 1회                              │
│   → 기존 safe executor                              │
│   → 실패 시 repair LLM 최대 1회                     │
└──────────────────────────────────────────────────────┘
        ↓
결과 저장
        ↓
┌──────────────────────────────────────────────────────┐
│ Hybrid Answer Builder                                │
│ Fast → 검증된 결과 계약 기반 고정 답변              │
│ Complex → 기존 answer LLM 1회                       │
└──────────────────────────────────────────────────────┘
        ↓
Message/API/Session/Runtime Cleanup
```

### 3.1 Langflow 분기 구현 방식

단순 Branch 노드는 사용하지 않는다. Langflow에서 중단된 branch가 공통 하위 노드를 재귀적으로 비활성화할 수 있으므로 Fast와 Complex가 다시 하나의 shared descendant로 합쳐지는 구조는 피한다.

대신 현재 pandas Repair가 실제 오류가 발생했을 때만 컴포넌트 내부에서 모델을 호출하는 방식과 같은 지연 호출 구조를 사용한다.

- `Hybrid Analysis Executor`가 route를 확인한 뒤 Complex일 때만 pandas 모델을 호출한다.
- `Hybrid Answer Builder`가 route를 확인한 뒤 Complex일 때만 answer 모델을 호출한다.
- 두 컴포넌트 모두 단일 `payload_out`을 반환한다.
- Fast Path에서도 Prompt Template 렌더링 같은 저비용 노드가 평가될 수는 있지만 Language Model 호출은 발생하지 않아야 한다.

## 4. LLM 호출 계약

| 경로 | Intent | pandas 생성 | Repair | 답변 | 정상 총 호출 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fast | 1 | 0 | 0 | 0 | 1 |
| Complex 성공 | 1 | 1 | 0 | 1 | 3 |
| Complex 최초 실행 실패 | 1 | 1 | 최대 1 | 1 | 최대 4 |
| 조회·계약 단계 차단 | 1 | 0 | 0 | 0 | 1 |

Repair는 기존과 같이 실제 pandas 실행 오류이면서 repair 가능한 오류에만 최대 한 번 호출한다. 조회 실패, 신뢰 카탈로그 오류, 컬럼 매핑 모호성, output contract 자체의 오류를 Repair LLM이 추측해서 고치게 하지 않는다.

## 5. Simple Analysis Contract

Fast Path는 질문 문자열이 아니라 정규화된 실행 계약으로 판정한다.

```json
{
  "version": 1,
  "route": "fast",
  "recipe": "ranked_summary",
  "source_alias": "production_today",
  "dataset_key": "production_today",
  "filters": [
    {
      "canonical_field": "OPER_NAME",
      "operator": "in",
      "typed_values": ["D/A1", "D/A2"],
      "value_type": "string",
      "execution_stage": "post_retrieval"
    }
  ],
  "projection": [],
  "group_by": ["TECH", "DEN", "MODE"],
  "metrics": [
    {
      "source_column": "PRODUCTION",
      "aggregation": "sum",
      "output_column": "PRODUCTION_SUM"
    }
  ],
  "post_filters": [],
  "ordering": [
    {
      "column": "PRODUCTION_SUM",
      "direction": "desc"
    }
  ],
  "limit": 3,
  "tie_policy": "first_n",
  "null_policy": {
    "dimensions": "preserve_as_blank",
    "metrics": "display_zero"
  },
  "result_columns": ["TECH", "DEN", "MODE", "PRODUCTION_SUM"],
  "eligibility": {
    "eligible": true,
    "reason_codes": ["single_source", "supported_recipe", "schema_resolved"]
  }
}
```

이 계약은 새로운 LLM 출력 형식을 강제로 늘리기보다 기존 항목으로부터 결정론적으로 구성한다.

- `retrieval_jobs`
- `condition_resolution.effective_filters`
- `pandas_execution_plan`
- `resolved_execution_graph`
- `resolved_grain_plan`
- `output_contract`
- 실제 조회된 source schema

LLM이 `route=fast`를 선언하더라도 신뢰하지 않는다. 최종 route는 Resolver가 실제 계약과 schema를 검증해 결정한다.

## 6. Fast Path 레시피

### 6.1 기본 레시피

| recipe | 지원 형태 | 허용 실행 단계 |
| --- | --- | --- |
| `detail_query` | 조건에 맞는 상세/목록 | filter → select → sort → limit |
| `scalar_summary` | count, sum, mean, median, min, max | filter → scalar aggregate |
| `group_summary` | 공정별·제품별·장비별 집계 | filter → groupby → aggregate |
| `ranked_summary` | 상위/하위 N, 최대/최소 항목 | groupby → aggregate → sort → limit |
| `frequency_summary` | 값별 빈도와 건수 | filter → value count |
| `distinct_summary` | 고유값 목록 또는 종류 수 | filter → unique/nunique |
| `list_summary` | group별 중복 없는 ID 목록 | filter → groupby → collect_unique |
| `existence_summary` | 데이터 존재 여부 | filter → count/any |
| `quality_summary` | null·blank·중복 건수/비율 | fixed quality operators |
| `latest_earliest` | 최신·최초·가장 오래된 항목 | filter → explicit sort → first/last |

기본 집계는 현재 deterministic executor와 동일한 범위를 우선 사용한다.

- `sum`
- `mean`
- `median`
- `min`
- `max`
- `count`
- `nunique`
- `first`
- `last`
- `collect_unique`

### 6.2 고급 결정론적 레시피

다음 레시피도 V2 최초 구현과 검증 범위에 함께 포함한다. 다만 기본 레시피보다 필요한 계약이 많으므로 내부 구현 순서만 뒤에 두며, 아래 필수 계약이 완성된 경우에만 Fast Path로 실행한다.

| recipe | 필수 추가 계약 |
| --- | --- |
| `percent_of_total` | 분모 범위, 0 나누기 정책 |
| `rank_within_group` | partition, ordering, tie policy |
| `threshold_after_aggregate` | 집계 후 조건과 타입 |
| `time_bucket_summary` | 시간 컬럼, 주기, closed/label |
| `period_change` | 현재/비교 기간, 증감률 분모 정책 |
| `running_total` | partition과 정렬 |
| `moving_aggregate` | window, min periods, 정렬 |
| `percentile_summary` | continuous/discrete와 interpolation |
| `pivot_summary` | index, columns, values, aggregation |

### 6.3 고급 레시피 계약

고급 레시피는 다음 필드를 명시적으로 가져야 한다.

```json
{
  "calculation": {
    "partition_by": [],
    "order_by": [],
    "denominator_scope": "grand_total|partition_total",
    "zero_division_policy": "zero|null",
    "rank_method": "row_number|rank|dense_rank",
    "tie_policy": "first_n|include_all",
    "time_column": "",
    "frequency": "day|week|month|quarter|year",
    "closed": "left|right",
    "label": "left|right",
    "periods": 1,
    "window": 0,
    "min_periods": 1,
    "percentile": 0.5,
    "percentile_method": "continuous|discrete",
    "pivot_index": [],
    "pivot_columns": [],
    "pivot_values": [],
    "pivot_aggregation": "sum",
    "max_pivot_columns": 50
  }
}
```

레시피별 실제 필수 필드는 Resolver가 별도로 검증한다.

- `percent_of_total`: metric, 분모 범위, partition, 0 나누기 정책
- `rank_within_group`: partition, 정렬 metric/direction, rank method, 동률 정책
- `threshold_after_aggregate`: 집계된 output column, 비교 연산자, typed threshold
- `time_bucket_summary`: 단일 source의 시간 컬럼, frequency, timezone, `closed`, `label`
- `period_change`: 단일 source 안의 기간 컬럼, partition, order, periods, difference/percent 방식
- `running_total`: partition, order, 누적 metric
- `moving_aggregate`: partition, order, window, min periods, 집계 방식
- `percentile_summary`: metric, percentile 범위 0~1, continuous/discrete 정책
- `pivot_summary`: index, columns, values, 집계, fill 정책, 최대 동적 컬럼 수

고급 레시피의 공통 제한은 다음과 같다.

- 기간 비교도 하나의 조회 결과 안에 필요한 기간이 모두 포함된 경우에만 Fast Path로 실행한다. 두 retrieval source를 결합해야 하면 Complex Path로 보낸다.
- 시계열 정렬 컬럼의 값이 날짜/시간으로 변환되지 않으면 추정하지 않고 Complex 또는 계약 오류로 처리한다.
- pivot 결과는 `result_schema_mode=derived_bounded`로 표시하고, 실제 고유값으로 만든 컬럼을 안정적으로 정렬하며 `max_pivot_columns`를 넘으면 Complex Path로 보낸다.
- 분모가 0인 비율, 첫 기간의 증감률, window의 최소 관측치 부족은 계약에 정의된 null/zero 정책만 적용한다.
- Resolver가 기본값을 임의로 선택하지 않는다. Domain 또는 정규화된 실행 계획에 필요한 값이 없으면 Fast 대상이 아니다.

## 7. 결정론적 필터 처리

필터는 Fast Path의 필수 공통 단계이며 pandas 생성 LLM에 맡기지 않는다.

### 7.1 처리 순서

```text
intent의 field
 → 메인 canonical filter 확인
 → 선택된 table catalog의 filter_mappings 확인
 → source 표준 컬럼화
 → canonical field 기준 고정 filter 실행
```

### 7.2 최초 지원 연산자

- 동일/포함: `eq`, `in`, `ne`, `not_in`
- 숫자 비교: `gt`, `ge`, `lt`, `le`
- 문자열: `contains`, `starts_with`, `ends_with`
- 결측/빈 값: `is_null`, `not_null`, `is_empty`, `not_empty`, `not_blank`, `null_or_empty`
- 제한된 복합 조건: `or`, `any`
- 여러 condition의 기본 결합: AND

`between`은 별도 실행기를 만들지 않고 `ge`와 `le` 두 condition으로 정규화한다. 정규식, 임의 Python 표현식, 사용자 정의 lambda는 최초 V2 Fast Path에서 허용하지 않는다.

### 7.3 조회 조건과 pandas 조건 분리

각 필터에는 실행 위치와 적용 상태를 기록한다.

| 실행 위치 | 예 | 처리 |
| --- | --- | --- |
| `retrieval_param` | Oracle `DATE` 파라미터 | retriever가 실행 |
| `retrieval_pushdown` | source가 지원하는 직접 조건 | retriever가 실행 |
| `post_retrieval` | `OPER_NAME`, 제품 속성, 수량 조건 | 표준화 후 filter executor가 실행 |

이미 적용된 조건을 다시 적용하지 않는다. 특히 조회 파라미터 `DATE`를 실제 결과에 존재하지 않는 `WORK_DATE`로 재적용하는 문제를 방지한다.

### 7.4 필터 trace

```json
{
  "source_alias": "production_today",
  "canonical_field": "MCP_NO",
  "operator": "starts_with",
  "values": ["L-267"],
  "execution_stage": "post_retrieval",
  "status": "applied",
  "row_count_before": 1200,
  "row_count_after": 32
}
```

고정 답변은 `status=applied`인 조건만 사용자에게 적용 기준으로 설명한다.

## 8. Fast Path 판정

### 8.1 Fast 허용 조건

다음을 모두 만족해야 한다.

1. 필수 외부 source가 정확히 하나다.
2. source 조회가 성공했으며 실제 schema를 확인할 수 있다.
3. 실행 graph가 하나의 source에서 시작하는 선형 또는 단일-source 파생 graph다. 기간 비교도 하나의 source 안에서 수행되어야 한다.
4. 모든 filter, grain, metric, sort 컬럼이 canonical schema로 유일하게 해석된다.
5. 모든 연산과 집계가 allowlist 안에 있다.
6. 결과 grain, metric, 컬럼 순서가 `output_contract`로 확정된다.
7. null, 정렬, limit, 동률, 분모, window, 시간 구간 정책이 필요한 경우 모두 명시된다.
8. function case, 임의 계산식, 자유 Python 코드가 필요하지 않다.
9. 결과 크기가 설정된 상세 조회 제한 이내다.

### 8.2 Complex로 보내는 조건

- 둘 이상의 외부 source 사용
- join, outer population 보존, 존재·부재 비교
- 계획 대비 실적 등 서로 다른 metric source 비교
- 사용자 정의 수식이나 여러 단계의 파생 계산
- 제품 토큰 매칭 등 선택된 Function Case 실행
- 이전 결과의 행을 신규 source에 매칭하는 후속 enrich
- 시계열 순서·분모·window 계약이 불완전한 계산
- 자유 형식 설명이나 해석이 결과의 핵심인 요청

### 8.3 차단해야 하는 조건

다음은 Complex LLM으로 우회하지 않고 계약 오류로 차단한다.

- 모르는 dataset key
- trusted catalog hydration 실패
- 필수 조회 파라미터 누락
- 한 canonical 컬럼이 여러 실제 컬럼으로 모호하게 매핑됨
- output metric과 retrieval source lineage 불일치
- 필수 source 조회 실패

### 8.4 Runtime fallback

- Resolver 단계의 단순한 `fast_path_ineligible`은 오류가 아니라 Complex route 선택이다.
- 실제 schema 불일치나 catalog 계약 오류는 fail-closed 처리한다.
- Fast executor 자체의 예상하지 못한 내부 오류만 `fallback_to_complex_on_internal_error` 입력으로 Complex 재시도를 허용한다.
- 이 입력은 노드에서 확인 가능하게 제공하고 기본값은 초기 검증 단계에서 `false`로 둔다. 오류를 숨기지 않고 원인을 먼저 수집한 뒤 운영 정책을 결정한다.

## 9. 결정론적 실행기

Fast executor는 질문 원문을 읽지 않고 `simple_analysis_contract`만 실행한다.

실행 순서는 다음으로 고정한다.

1. source 존재와 schema 확인
2. canonical filter 적용
3. projection 또는 group grain 확정
4. 숫자 metric 타입 변환
5. 집계
6. post-filter
7. 정렬과 limit
8. 구성비·순위·시계열·window 계산
9. pivot 또는 bounded reshape
10. null policy
11. fixed 또는 derived-bounded result column projection
12. semantic execution certificate 생성

실제 결과에 없는 metric을 0으로 새로 만들어 성공으로 처리하지 않는다. 정상 조회 결과가 0행인 경우와 source 누락·실패를 반드시 구분한다.

## 10. 고정 답변 생성기

Fast Path에서는 답변 LLM을 호출하지 않는다. 답변은 recipe와 실제 실행 certificate로 생성한다.

### 10.1 답변 유형

| result mode | 기본 문장 |
| --- | --- |
| detail/entity list | `조건을 만족한 결과는 총 N건입니다.` |
| scalar | `{표시 metric}은 {값}입니다.` |
| aggregate | `{grain 표시명} 기준으로 집계한 결과는 N개 그룹입니다.` |
| ranking | `{metric 표시명} 기준 상위/하위 N개 결과입니다.` |
| existence | `조건을 만족하는 데이터가 있습니다/없습니다.` |
| quality | `검사 대상 N건 중 결측/중복은 M건입니다.` |
| ratio/rank | `{분모 또는 partition 기준} 구성비/순위 결과입니다.` |
| time series | `{시간 주기} 기준 집계·증감·누적 결과입니다.` |
| pivot | `{행 기준}과 {열 기준}으로 재구성한 집계 결과입니다.` |

### 10.2 답변 안전 규칙

- 컬럼명은 `column_labels`만 사용한다.
- 적용 기준은 실제 filter certificate에 `applied`로 기록된 조건만 표시한다.
- 상위 N, 존재·부재, 비교 조건은 실행 결과가 해당 계약을 통과했을 때만 서술한다.
- 0건, 조회 실패, 분석 실패를 서로 다른 문장으로 표현한다.
- dummy/live 구분, 다운로드 링크, 원본 source 정보는 기존 응답 계약을 유지한다.
- 결과 표의 row source는 기존과 같이 `data.rows` 하나만 사용한다.

## 11. Payload와 trace

기존 `docs/V5_PAYLOAD_CONTRACT.md`의 단일 소유 원칙을 유지하고 다음 항목만 추가한다.

```json
{
  "analysis": {
    "execution_route": "fast",
    "fast_path_recipe": "ranked_summary"
  },
  "trace": {
    "inspection": {
      "fast_path": {
        "eligible": true,
        "selected_route": "fast",
        "reason_codes": [],
        "filter_execution": [],
        "llm_calls": {
          "intent": 1,
          "pandas_generation": 0,
          "repair": 0,
          "answer": 0
        },
        "timing_ms": {
          "route_resolution": 0,
          "analysis_execution": 0,
          "answer_build": 0
        }
      }
    }
  }
}
```

`simple_analysis_contract` 전체를 여러 payload 위치에 복제하지 않는다. runtime에서는 단일 위치에 두고 API cleanup 단계에서는 운영 진단에 필요한 요약만 남긴다.

## 12. 구현 파일 계획

### 12.1 신규 전용 source

```text
langflow_components/data_analysis_flow_v2/
  14b_simple_analysis_contract_resolver.py
  17_hybrid_analysis_executor.py
  20_hybrid_answer_builder.py
  CONNECTION_GUIDE.md
```

- `14b`: 실제 조회 schema를 바탕으로 Fast eligibility와 실행 계약 확정
- `17`: Fast 고정 실행 또는 Complex pandas 모델 지연 호출 및 기존 safe execution/repair 수행
- `20`: Fast 고정 답변 또는 Complex answer 모델 지연 호출
- 변경하지 않는 loader, normalizer, hydrator, retriever, 저장·API component는 기존 source를 builder가 그대로 embed

### 12.2 Builder와 산출물

```text
tools/build_data_analysis_flow_v2.py
flow_exports/data_analysis_flow_v2_standalone.json
import_ready_flows/12_data_analysis_flow_v2_standalone.json
```

구현 시 다음 파일도 동기화한다.

- `import_ready_flows/manifest.json`
- `import_ready_flows/README_IMPORT.md`
- Flow/component source 동기화 검증 대상 목록
- 필요한 경우 validation tool의 Flow 선택 옵션

V2 builder는 현재 v5 export를 donor로 읽을 수 있지만 v5 export를 수정하거나 덮어쓰지 않는다.

## 13. 단계별 구현 순서

### 단계 0. 기준선 고정

- 현재 branch와 commit 기록
- 기존 전체 테스트와 대표 질문 결과 보관
- 기존 Flow JSON hash와 component source sync 상태 확인
- 현재 질문별 LLM 호출 횟수와 단계별 시간을 기준선으로 기록

### 단계 1. V2 복제와 import 검증

- 별도 builder와 V2 Flow ID 생성
- 기존 Flow와 이름, endpoint, export 경로 분리
- 기능 변경 없이 먼저 동일 동작하는 V2 clone 생성
- Langflow 1.9.2 parse/import 검증

### 단계 2. Simple Analysis Contract Resolver

- 기존 plan과 실제 source schema에서 범용 계약 생성
- Fast/Complex/Blocked reason code 정의
- 질문 원문 기반 분기 금지 테스트 추가
- table catalog mapping 외 컬럼 fallback 금지 테스트 추가

### 단계 3. 결정론적 필터와 기본 레시피

- 필터 적용 위치와 중복 실행 방지
- 기본 레시피 10종 구현
- strict output contract와 certificate 연결
- 0행과 조회 실패 구분

### 단계 4. 고급 결정론적 레시피

- 구성비와 partition별 순위 구현
- 집계 후 threshold 구현
- time bucket과 기간 증감 구현
- 누적합과 이동 집계 구현
- percentile과 bounded pivot 구현
- 분모·정렬·window·동률·동적 schema 계약 누락 시 Complex 전환 검증

### 단계 5. pandas 모델 지연 호출

- 외부의 항상 실행되는 pandas Language Model 경로 제거
- Hybrid Executor에 visible ModelInput/API key 입력 제공
- Fast에서는 모델을 호출하지 않는 spy 테스트 추가
- Complex에서는 기존 prompt, safe execution, helper, repair 동작 보존

### 단계 6. 답변 모델 지연 호출

- Fast 답변 템플릿 구현
- Complex에서만 answer model 호출
- 결과 저장, 다운로드, Message/API envelope 유지
- 답변이 실행되지 않은 조건을 주장하지 않는지 검증

### 단계 7. JSON과 문서 동기화

- builder 실행
- export/import-ready bundle 갱신
- V2 Connection Guide 작성
- manifest와 import 안내 갱신

### 단계 8. 회귀·성능 검증

- 단위, component, Flow import, Python 모방, 실제 LLM 검증
- Fast/Complex 경로별 호출 횟수와 응답 시간 비교
- 기존 Flow 결과와 의미·조회·결과 계약 단위 비교

## 14. 테스트 계획

### 14.1 단위 테스트

1. Fast recipe별 정상 실행
2. 지원하지 않는 operation의 Complex 전환
3. canonical filter 매핑과 타입 변환
4. retrieval 적용 필터의 재적용 방지
5. 공백/null 정책
6. top/bottom과 동률 정책
7. 집계 후 threshold 조건
8. strict result column 순서
9. missing/ambiguous schema fail-closed
10. Fast 경로 모델 미호출
11. Complex 경로 pandas/answer 모델 호출
12. Repair 최대 1회
13. 구성비의 전체/partition 분모와 0 나누기 정책
14. partition별 rank와 동률 정책
15. time bucket의 timezone/closed/label 경계
16. period change의 첫 기간과 0 분모 처리
17. running/moving window의 정렬과 min periods
18. continuous/discrete percentile
19. bounded pivot 컬럼 정렬과 최대 컬럼 초과 시 Complex 전환

### 14.2 대표 Fast 질문군

- 조건 적용 상세 목록
- 공정별/제품별 합계와 평균
- 전체 및 그룹별 count/nunique
- 상위·하위 N
- max/min 항목
- 고유값 및 LIST
- null/blank/중복 검사
- 최신·최초 항목
- `starts_with`, `contains`, 숫자 비교 필터
- 공정 그룹과 제품 Domain이 펼쳐진 필터
- 전체 및 그룹 내 구성비
- 그룹별 순위와 동률 처리
- 집계 결과의 threshold 필터
- 일·주·월 단위 시계열 집계
- 전기 대비 증감량과 증감률
- 누적합과 이동평균
- percentile 결과
- 제한된 pivot/crosstab 결과

### 14.3 반드시 Complex여야 하는 질문군

- 생산실적과 WIP의 제품별 outer join
- 계획 대비 실적 비교
- INPUT 실적 존재/D/A WIP 부재 비교
- 상위 제품 결과와 장비 배정 데이터 결합
- 이전 결과 행 기반 후속 enrich
- 제품 토큰 Function Case가 필요한 질문
- 두 날짜·두 source가 필요한 생산실적과 BOH 비교

### 14.4 전체 질문셋

- `validation_questions.txt`
- `docs/DATA_ANALYSIS_CURRENT_VALIDATION_QUESTION_SET_20260729.md`
- 기존 멀티턴 검증셋

검증은 질문별 문장이나 `analysis_kind` 문자열을 고정 비교하지 않는다. 다음을 우선 비교한다.

- dataset/source 선택
- temporal semantics와 실제 조회일
- effective filter와 적용 여부
- output grain과 metric binding
- 실행 route의 적합성
- 결과 컬럼과 실제 값
- 모델 호출 횟수
- 다운로드 및 API envelope

### 14.5 실제 LLM 검증

- 사용자가 승인한 환경에서 `gemini-3.5-flash-lite`로 의도 분석부터 실제 경로까지 실행한다.
- Fast 대상 질문은 pandas/answer model 호출이 0인지 trace로 확인한다.
- Complex 질문은 기존 pandas 생성·repair·답변 경로가 유지되는지 확인한다.
- 같은 질문을 반복 실행하여 route와 결과 schema가 안정적인지 확인한다.

### 14.6 Langflow 검증

- `tools/validate_flow_component_sources.py`
- 정확한 Langflow 1.9.2 / langflow-base 0.9.2 / LFX 0.4.2 runtime parse
- 기존 생성 Flow 전체와 신규 V2 Flow의 `last_tested_version=1.9.2`
- 모든 직렬화 node의 `lf_version=1.9.2`
- V2 import-ready JSON을 새 Flow로 실제 import
- standalone 환경에서 sibling Python import 없이 실행

## 15. 성능 검증 기준

절대 응답 시간은 모델과 source 환경의 영향을 받으므로 호출 횟수와 단계별 시간을 함께 측정한다.

| 측정 항목 | 기대 결과 |
| --- | --- |
| Fast pandas generation 호출 | 0 |
| Fast answer 호출 | 0 |
| Fast repair 호출 | 0 |
| Fast 결과 값 | 동일 계약의 deterministic pandas 결과와 일치 |
| Complex 기능 | 기존 Flow와 의미·조회·결과 계약 동등 |
| route resolution | LLM 호출 없이 수행 |
| trace | route, reason, filter rows, LLM 호출 수 확인 가능 |

운영 판단에는 최소 p50/p95를 분리해 기록한다.

- Intent 시간
- Retrieval 시간
- Route/Filter/Analysis 시간
- Answer 시간
- 전체 시간

## 16. 완료 기준

다음을 모두 만족해야 구현 완료로 판단한다.

1. 기존 Data Analysis Flow와 원본 JSON에 기능 변경이 없다.
2. V2가 별도 Flow로 import되고 standalone 실행된다.
3. 기본 및 고급 Fast recipe 전체와 고정 필터가 질문 특화 하드코딩 없이 실행된다.
4. Fast 질문에서 pandas·answer LLM 호출이 모두 0이다.
5. Complex 질문에서 기존 pandas·repair·answer 기능이 유지된다.
6. 모든 source/컬럼/시간/단위 규칙이 metadata에서 해석된다.
7. 컬럼 매핑이 없을 때 유사 이름 fallback을 사용하지 않는다.
8. 결과가 0건인 경우와 조회 실패가 구분된다.
9. 기존 질문셋과 멀티턴 질문의 의미·조회·결과 계약 회귀가 없다.
10. Python source, Flow export, import-ready JSON이 동기화된다.
11. Langflow 1.9.2 정확한 조합에서 parse 및 import 검증을 통과한다.
12. Fast/Complex 선택 이유와 모델 호출 횟수를 trace에서 확인할 수 있다.
13. 구성비·기간 비교·window·percentile·pivot에 필요한 계약이 없을 때 Fast Path가 임의 기본값을 사용하지 않는다.
14. 고급 레시피도 단일 source 조건을 지키며 다중 source가 필요하면 기존 Complex Path로 처리된다.

## 17. 구현 중 금지 사항 체크리스트

- [ ] 질문 문구별 `if` 또는 regex를 공통 router에 추가하지 않는다.
- [ ] 공정·제품·특정 dataset key를 Fast executor에 직접 쓰지 않는다.
- [ ] `DEN/DENSITY` 같은 alias 목록을 공통 fallback으로 추가하지 않는다.
- [ ] source에 없는 metric을 다른 metric 복사 또는 임의 0으로 생성하지 않는다.
- [ ] 조회 실패를 0건으로 바꾸지 않는다.
- [ ] Fast 판정을 Intent LLM의 선언만으로 신뢰하지 않는다.
- [ ] Fast 실행 오류를 무조건 LLM 코드로 덮어 숨기지 않는다.
- [ ] runtime sibling import로 standalone 조건을 깨지 않는다.
- [ ] 기존 Flow export를 V2로 덮어쓰지 않는다.
- [ ] 검증 결과 JSON과 runtime 임시 산출물을 커밋하지 않는다.

## 18. V2 최초 구현 범위

V2 최초 완료본에는 다음 범위를 모두 포함한다. 안정적인 개발을 위해 기본 레시피를 먼저 완성한 뒤 고급 레시피를 구현하지만, 고급 레시피도 완료·회귀 검증을 통과해야 V2 구현을 완료한 것으로 판단한다.

1. 단일 source
2. canonical filter
3. detail/select/sort/limit
4. count, sum, mean, median, min, max, nunique, collect_unique
5. group summary
6. top/bottom N과 max/min row
7. null/blank/duplicate 검사
8. Fast 고정 답변
9. Complex 기존 경로 보존
10. 전체 및 partition 기준 구성비
11. partition별 rank와 동률 정책
12. 집계 후 threshold
13. 일·주·월·분기·연 단위 time bucket
14. 전기 대비 증감량과 증감률
15. 누적합과 이동 집계
16. continuous/discrete percentile
17. bounded pivot/crosstab

고급 레시피는 질문별 특화 로직으로 구현하지 않는다. 모든 계산은 `simple_analysis_contract.calculation`의 범용 필드와 실제 table catalog schema를 사용하고, 필수 계약이 빠진 질문만 기존 Complex Path로 보낸다.
