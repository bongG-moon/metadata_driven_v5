# Route Flow V4 예시

## 1. 단일 Tool Workflow

```json
{
  "workflow_key": "metadata_domain_lookup",
  "title": "도메인 메타데이터 조회",
  "steps": [
    {
      "step_id": "lookup_domain",
      "tool_name": "run_metadata_qa",
      "question": "{{user_question}}",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    }
  ]
}
```

단일 질문의 속도가 중요하면 Route V2가 우선입니다. 이 예시는 V4 Registry 형식 확인용입니다.

## 2. 현재 HOLD LOT 조회 후 HOLD 이력 분석

사용자 질문 예시:

```text
현재 HOLD 중인 LOT을 조회하고 해당 LOT의 HOLD 이력과 주요 원인을 알려줘.
```

```json
{
  "workflow_key": "hold_lot_history_metadata_audit",
  "title": "현재 HOLD LOT 이력과 데이터 정의 감사",
  "steps": [
    {
      "step_id": "current_hold_lots",
      "tool_name": "run_data_analysis",
      "question": "현재 HOLD 중인 LOT을 조회하고 LOT_ID를 결과에 포함해줘.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    },
    {
      "step_id": "hold_history",
      "tool_name": "run_data_analysis",
      "question": "이전 결과에 포함된 LOT의 HOLD 이력, HOLD 코드, 주요 사유를 분석해줘.",
      "depends_on": ["current_hold_lots"],
      "handoff": "result_ref",
      "on_error": "stop"
    }
  ]
}
```

첫 Tool은 `result_ref`를 생성해야 하고 두 번째 Tool은 `upstream_result_ref`를 지원해야 합니다.

## 3. 독립 조회를 계속하는 4단계 Workflow

```json
{
  "workflow_key": "daily_operations_brief",
  "title": "일일 운영 종합",
  "steps": [
    {
      "step_id": "production",
      "tool_name": "run_data_analysis",
      "question": "{{user_question}} 기준 생산량을 분석해줘.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "continue"
    },
    {
      "step_id": "wip",
      "tool_name": "run_data_analysis",
      "question": "{{user_question}} 기준 재공을 분석해줘.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "continue"
    },
    {
      "step_id": "holds",
      "tool_name": "run_data_analysis",
      "question": "{{user_question}} 기준 HOLD 현황을 분석해줘.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "continue"
    },
    {
      "step_id": "metadata_note",
      "tool_name": "run_metadata_qa",
      "question": "앞의 운영 지표를 해석할 때 적용되는 주요 도메인과 집계 기준을 설명해줘.",
      "depends_on": ["production", "wip", "holds"],
      "handoff": "none",
      "on_error": "continue"
    }
  ]
}
```

마지막 단계는 세 앞 단계와 실행 순서만 연관되며 ref를 전달하지 않습니다. 앞 단계 하나가 실패하면 dependency 규칙으로 마지막 단계는 `blocked`가 되지만, 서로 독립인 앞 단계들은 계속 실행됩니다.

## 4. Registry에서 key만 실행

Workflow Skill 저장 Flow로 MongoDB Registry에 저장했거나 `00A`를 명시적 `inline_seed`로 전환한 뒤 다음 중 하나를 사용합니다.

- `workflow_key` 입력: `hold_lot_history_metadata_audit`
- Inline Workflow 정의: `workflow_key: hold_lot_history_metadata_audit`
- Inline Workflow 정의: `hold_lot_history_metadata_audit`

`00A`는 현재 질문과 관련된 후보만 최대 8개로 줄이고, 명시 key는 후보 Registry에서 정확히 하나를 찾아야 합니다. `mongodb` 모드 조회가 실패해도 inline seed로 자동 전환하지 않습니다.

## 5. 등록하지 않은 조회·HTML 시각화 조합

```text
최근 3일 D/A 공정 생산량을 조회하고 선 그래프로 그려줘.
```

일치하는 Skill이 없어도 계획 모델은 Tool capability catalog 안에서 다음 `workflow_key=inline` 순서를 만들 수 있습니다.

```text
run_data_analysis (depends_on=[], handoff=none)
-> run_visualization (depends_on=[production], handoff=result_ref)
-> 최종 Language Model 1회
```

`run_visualization`은 첫 단계로 호출하지 않으며, 앞 분석 결과의 전체 행을 질문에 복사하지 않고 `result_ref`로 전달합니다.

## 6. 차단되는 잘못된 예시

### 5단계 초과

`steps`가 5개이면 `workflow_step_limit_exceeded`로 Loop 실행 전에 차단됩니다.

### 미래 단계 참조

```json
{
  "step_id": "first",
  "depends_on": ["later"]
}
```

`future_or_unknown_dependency` 오류입니다.

### 모호한 ref 전달

```json
{
  "step_id": "combined",
  "depends_on": ["a", "b"],
  "handoff": "result_ref"
}
```

한 입력에 어느 ref를 전달할지 추측하지 않고 `ambiguous_result_ref_handoff`로 차단합니다.

### Tool 이름 오타

`Allowed Tool Names`가 설정된 경우 미등록 이름은 `unregistered_tool_name` 오류입니다. 실행기에 같은 이름의 Tool이 없거나 둘 이상이어도 각각 `tool_not_found`, `duplicate_tool_name`으로 차단됩니다.
