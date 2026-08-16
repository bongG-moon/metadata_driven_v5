# Report Follow-up Flow 설계

## 결정

Report 생성 결과에 대한 snapshot 내부 후속 분석은 Data Analysis Flow 01과 분리하여 `10. v5_report_followup`에서 처리한다.

- Flow 07: Report 생성, 조회용 snapshot View 저장, active Report context 발행
- Flow 10: 같은 세션의 저장된 Report View에 대한 필터·정렬·상위 N·컬럼 선택
- Flow 11: 후속 Context가 없던 변경 전 Report 직접 응답 구조 보존
- Flow 01: 현재·최신·재조회, snapshot에 없는 데이터, 다른 데이터셋 결합
- Flow 06: 사용자 질문을 Flow 10 또는 Flow 01 중 정확히 하나로 라우팅

Flow 10은 Table Catalog, Main Filter, Oracle/Goodocs 조회, retrieval job, join, 임의 pandas 코드 생성 및 repair를 포함하지 않는다.

## 설계 원칙

1. Report의 실제 물리 컬럼이 권위 원본이다. 전역 `DEN`, `PKG_TYPE1` 같은 표준 컬럼을 강제하지 않는다.
2. Report 생성 Flow가 후속 분석 가능한 Materialized View를 snapshot 문서에 함께 저장한다.
3. Flow 10은 선언된 View와 허용 연산만 실행하는 작은 Typed 계획을 사용한다.
4. 원본 행은 LLM에 전달하지 않는다. LLM은 질문과 제한된 View/컬럼 계약만 본다.
5. 만료·누락·다른 세션·불완전 저장은 모두 fail-closed 한다.
6. Flow 10은 외부 재조회로 자동 전환하지 않는다. 최신/외부 데이터 요청은 Router가 Flow 01로 보낸다.
7. 첫 후속 결과가 원본 Report context를 덮어쓰지 않는다. `그중` 후속은 직전 View 계획을 원본 snapshot에 다시 적용한다.
8. 새 Report와 오래 실행된 후속 질문이 경합할 때 과거 context가 되살아나지 않도록 세션 저장에 CAS guard를 사용한다.

## Flow 10 그래프

```text
Chat Input
  -> 공용 Session State Loader
  -> Report Follow-up Prompt Builder
  -> Guarded Plan Router (ready일 때만 LLM 1회)
  -> Report Follow-up Plan Normalizer
  -> 공용 MongoDB Result Loader
  -> Report Snapshot Executor
  -> Report Follow-up Response Builder
  -> 공용 Session State Writer (CAS guard)
  -> Report Follow-up API Terminal
  -> Chat Output
```

Guarded Plan Router는 context 누락·만료·확인 필요·신규 조회 위임 상태에서 모델을 호출하지 않는다. `ready` 요청에서만 모델을 최대 1회 호출하며, Plan Normalizer는 모델 결과를 그대로 실행하지 않고 source alias, 컬럼, 연산, 값, 정렬 방향과 limit를 snapshot 계약으로 다시 검증한다.

## Report 조회 View 계약

각 Report는 필요한 View만 선택적으로 발행한다. 공통 필수 필드는 다음과 같다.

```json
{
  "contract_version": "report.query_source.v1",
  "purpose": "production_shortage_products",
  "source_alias": "report_shortage_products",
  "dataset_key": "report_shortage_products",
  "authoritative": true,
  "grain": {
    "kind": "product",
    "columns": ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"],
    "unique": true
  },
  "aliases": ["생산부족 제품", "생산 부족 제품", "부족 제품"],
  "columns": ["실제 물리 컬럼"],
  "allowed_operations": ["filter", "sort_and_top_n", "select_columns"]
}
```

Flow 07은 우선 다음 View를 저장한다.

- `report_snapshot`: Report의 제품·공정 Case 상세
- `report_shortage_products`: `달성율*판정=생산부족` Case를 Flow 07의 실제 제품 키로 집계한 제품 View

제품 View의 `PRODUCTION`과 `OUT_PLAN`은 합산하고, `생산실적달성율`은 `PRODUCTION / OUT_PLAN * 100`으로 다시 계산한다. 비율 평균은 사용하지 않는다.

## 실행 계획 계약

```json
{
  "contract_version": "report.followup.plan.v1",
  "status": "ready",
  "source_view_key": "production_shortage_products",
  "source_alias": "report_shortage_products",
  "operations": [
    {"operation": "sort", "column": "생산실적달성율", "direction": "asc", "nulls": "last"},
    {"operation": "top_n", "limit": 5},
    {"operation": "select", "columns": ["View가 허용한 실제 컬럼"]}
  ]
}
```

허용되지 않는 source, join, retrieval, 미등록 컬럼, 임의 표현식, 100을 초과하는 limit는 실행하지 않는다.

## 상태 계약

세션에는 원본 Report anchor와 파생 계획을 구분해 저장한다.

```text
current_data.report_context.context_ref  # 원본 Report anchor
current_data.current_view_plan              # 직전 파생 View 계획
_session_state_revision                    # 내부 CAS revision
```

- `방금 Report에서`는 원본 snapshot의 선언된 query source를 기준으로 새 계획을 만든다.
- `그중`, `위 결과에서`는 `current_view_plan`의 source를 우선 이어받고 새 요청을 적용한다.
- 파생 결과 전체 행은 세션 state에 저장하지 않는다.

Flow 10 저장 시 로드 당시 `turn_count`와 `context_ref`가 현재 세션 문서와 모두 일치할 때만 상태를 교체한다. 불일치하면 결과를 새 active context로 저장하지 않고 재시도 안내를 반환한다.

## Router 경계

| 질문 유형 | 대상 |
| --- | --- |
| 방금/위 Report 내부 필터·정렬·상위 N | Flow 10 |
| `그중`으로 직전 Report View를 이어서 분석 | Flow 10 |
| 현재·최신·다시 조회·재조회 | Flow 01 |
| 다른 데이터셋과 결합 또는 snapshot에 없는 지표 | Flow 01 |
| Report context가 없는 Report 참조 | Flow 10에서 재조회 없이 안내 |

## 필수 검증

- 문제 질문이 외부 조회 0건으로 제품 View를 선택하고 달성율 오름차순 5행을 반환한다.
- 결과 제품 키는 중복되지 않고 모두 Report 당시 생산부족 Case에서 유래한다.
- null 달성율은 마지막으로 정렬하며 숫자 정렬을 사용한다.
- Report가 `DENSITY`, `PKG1`, `PKG2`를 사용해도 전역 표준 키를 요구하지 않는다.
- context 누락·만료·다른 session·불완전 snapshot을 차단한다.
- 첫 후속 뒤 두 번째 `그중 3개만`이 원본 Report anchor를 유지한다.
- 새 Report 생성과 이전 후속 저장의 경합에서 이전 context가 복원되지 않는다.
- 현재/최신 요청과 외부 데이터 결합 요청은 Flow 10 executor에 도달하지 않는다.
- Flow 01의 기존 대표 질문이 회귀하지 않는다.
- Flow 11의 결과에는 `context_ref`, `followup`, `state`와 Context 미생성 경고가 없고, Flow 07만 Snapshot을 저장한다.
- source, export, import-ready, combined bundle, manifest, ZIP이 9개 Flow 및 Langflow 1.11.0으로 동기화된다.
