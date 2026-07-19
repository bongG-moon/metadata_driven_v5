# DA 일일 생산·재공 브리핑

## 문서 정보

- Workflow key: `daily_manufacturing_briefing`
- 목적: 오늘 DA 공정 생산량과 현재 재공을 조회한 뒤 관련 데이터셋 정의를 함께 제공한다.
- 실행 단계: Data Analysis 2회, Metadata QA 1회
- 데이터 handoff: 사용하지 않음
- 최종 처리: 08의 기본 Language Model이 단계 결과를 한 답변으로 종합

## 실행 순서

1. `production`: `run_data_analysis`로 D/A1~D/A6 공정별 당일 생산량 조회
2. `wip`: `production` 완료 후 `run_data_analysis`로 D/A1~D/A6 현재 재공 조회
3. `metadata`: `wip` 완료 후 `run_metadata_qa`로 `production_today`, `wip_today` 정의 비교

모든 단계는 순차 실행하지만 앞 결과 행을 다음 단계 입력으로 사용하지 않으므로 `handoff=none`이다. 마지막 Metadata QA 실패만 `continue`로 처리해 실제 생산·재공 결과는 보존한다.

## 검증 질문

```text
daily_manufacturing_briefing
```

```text
오늘 DA 공정의 생산량과 현재 재공, 사용 데이터 소스를 함께 브리핑해줘.
```
