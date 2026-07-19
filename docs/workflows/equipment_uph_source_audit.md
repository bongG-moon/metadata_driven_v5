# D/A1 장비·UPH와 데이터 소스 감사

## 문서 정보

- Workflow key: `equipment_uph_source_audit`
- 목적: 현재 D/A1 장비와 장비 모델·Recipe별 UPH를 조회하고 관련 데이터셋 정의를 함께 제공한다.
- 실행 단계: Data Analysis 1회, Metadata QA 1회
- 데이터 handoff: 사용하지 않음
- 최종 처리: 08의 기본 Language Model이 단계 결과를 한 답변으로 종합

## 실행 순서

1. `equipment_uph`: `run_data_analysis`로 D/A1 장비 ID, 장비 모델, Recipe, 공정, UPH 조회
2. `source_metadata`: `equipment_uph` 완료 후 `run_metadata_qa`로 `equipment_assign`, `eqp_uph` 정의 비교

두 번째 단계는 실행 순서만 보장하며 첫 단계 결과 행을 소비하지 않으므로 `handoff=none`이다. Metadata QA가 실패하더라도 장비·UPH 결과를 제공할 수 있도록 `on_error=continue`를 사용한다.

## 검증 질문

```text
equipment_uph_source_audit
```

```text
현재 D/A1 장비와 장비 모델, Recipe별 UPH를 보여주고 어떤 데이터 소스를 사용했는지도 알려줘.
```
