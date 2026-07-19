# 현재 HOLD LOT 이력과 데이터 정의 감사

## 문서 정보

- Workflow key: `hold_lot_history_metadata_audit`
- 목적: 현재 HOLD 중인 LOT을 찾고 실제 조회된 LOT만 대상으로 HOLD 이력을 조회한 뒤 관련 데이터셋 정의를 함께 제공한다.
- 실행 단계: Data Analysis 2회, Metadata QA 1회
- 데이터 handoff: `current_hold_lots` 결과를 `hold_history`에 `result_ref`로 전달
- 최종 처리: 08의 기본 Language Model이 단계 결과를 한 답변으로 종합

## 실행 순서

1. `current_hold_lots`: `run_data_analysis`로 현재 HOLD 상태 LOT과 `LOT_ID` 조회
2. `hold_history`: `current_hold_lots` 완료 후 `run_data_analysis`로 해당 LOT의 HOLD 이력 조회
3. `metadata`: `hold_history` 완료 후 `run_metadata_qa`로 `lot_status`, `hold_history` 정의 비교

2단계만 앞 단계 실제 행이 필요하므로 `handoff=result_ref`를 사용한다. Metadata QA는 결과 행을 소비하지 않으므로 `handoff=none`이다.

## 사전 조건

- Data Analysis의 MongoDB Result Store가 live 저장 상태여야 한다.
- 두 Data Analysis 호출이 같은 부모 `session_id`를 사용해야 한다.
- `hold_history` 카탈로그에 `LOT_ID` 기반 `upstream_bindings`가 등록되어 있어야 한다.

## 검증 질문

```text
hold_lot_history_metadata_audit
```

```text
현재 HOLD 중인 LOT과 해당 LOT들의 HOLD 이력, 사용 데이터 소스를 함께 알려줘.
```
