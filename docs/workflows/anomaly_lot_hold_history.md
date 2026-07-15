# 이상 LOT과 HOLD 이력 연계 조회

## 문서 정보

- Workflow key: `anomaly_lot_hold_history`
- 목적: 이상 LOT을 먼저 찾고, 그 결과에 포함된 LOT만 대상으로 HOLD 이력을 조회한다.
- 실행 단계: 2개
- 데이터 handoff: `result_ref` 사용
- 기본 오류 정책: 첫 단계 또는 handoff 실패 시 중단

## 업무 요청 예시

```text
오늘 이상 LOT을 찾아서 해당 LOT들의 HOLD 이력을 알려줘.
```

## 단계 정의

### 1. 이상 LOT 조회

- `step_id`: `anomaly_lots`
- `tool_name`: `run_data_analysis`
- 질문: `오늘 이상 LOT을 조회하고 LOT_ID를 결과에 포함해줘.`
- 선행 단계: 없음
- handoff: `none`
- 실패 정책: `stop`

### 2. 선별 LOT의 HOLD 이력 조회

- `step_id`: `hold_history`
- `tool_name`: `run_data_analysis`
- 질문: `앞 단계에서 조회된 LOT만 대상으로 HOLD 이력을 조회해.`
- 선행 단계: `anomaly_lots`
- handoff: `result_ref`
- 실패 정책: `stop`

이 단계는 앞 단계의 전체 LOT 목록이 필요하다. 자연어 요약이나 ID 미리보기를 다시 질문에 복사하지 않고 MongoDB Result Store의 `result_ref`를 그대로 전달한다. Table Catalog에 해당 데이터셋의 `upstream_bindings`가 등록되어 있어야 실제 `LOT_ID` 조회 파라미터로 변환된다.

## 사전 조건

- 두 단계가 같은 부모 `session_id`를 사용해야 한다.
- Data Analysis의 MongoDB Result Store가 live 저장 상태여야 한다.
- 1단계 결과가 잘리지 않은 상태로 저장되어야 한다.
- HOLD 이력 데이터셋에 `LOT_ID` 기반 `upstream_bindings`가 등록되어 있어야 한다.

## 기대 답변

최종 답변은 이상 LOT 요약과 해당 LOT의 HOLD 이력을 한 번에 보여준다. ref가 없거나 복원할 수 없으면 전체 LOT을 추측해 조회하지 않고 실패 원인을 명시한다.
