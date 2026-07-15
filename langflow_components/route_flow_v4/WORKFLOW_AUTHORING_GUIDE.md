# Route Flow V4 Workflow 작성 가이드

## 기본 계약

등록 Workflow는 `workflow.plan.v1` 의미를 따르며 단계는 최대 4개입니다.

```json
{
  "workflow_key": "anomaly_lot_hold_history",
  "title": "이상 LOT 및 HOLD 이력 분석",
  "description": "이상 LOT을 찾은 뒤 해당 LOT의 HOLD 이력을 분석합니다.",
  "steps": [
    {
      "step_id": "find_abnormal_lots",
      "tool_name": "run_data_analysis",
      "question": "{{user_question}}에서 요청한 기준으로 이상 LOT을 분석해줘.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    },
    {
      "step_id": "analyze_hold_history",
      "tool_name": "run_data_analysis",
      "question": "이전 결과의 LOT을 대상으로 HOLD 이력과 주요 사유를 분석해줘.",
      "depends_on": ["find_abnormal_lots"],
      "handoff": "result_ref",
      "on_error": "stop"
    }
  ]
}
```

## 필드 규칙

| 필드 | 필수 | 규칙 |
| --- | --- | --- |
| `workflow_key` | Registry 사용 시 필수 | 영문·숫자·점·밑줄·하이픈 사용을 권장합니다. |
| `steps` | 필수 | 1~4개입니다. |
| `step_id` | 필수 | 영문자로 시작하고 영문·숫자·밑줄·하이픈만 사용합니다. Workflow 안에서 유일해야 합니다. |
| `tool_name` | 필수 | 실제 연결 Tool의 `name`과 완전히 같아야 합니다. |
| `question` | 필수 | 해당 Tool 하나가 수행할 구체적인 요청입니다. 최대 4,000자입니다. |
| `depends_on` | 필수 | 앞에서 정의된 `step_id`만 참조합니다. 첫 단계는 빈 배열입니다. |
| `handoff` | 필수 | `none` 또는 `result_ref`입니다. |
| `on_error` | 필수 | `stop` 또는 `continue`입니다. |

`handoff=result_ref`이면 `depends_on`은 정확히 1개여야 합니다. 두 결과를 하나의 ref 입력에 합치는 암묵적 fallback은 지원하지 않습니다.

## 오류 정책

- `on_error=stop`: 현재 단계가 `error` 또는 `blocked`이면 뒤 단계 Tool을 호출하지 않습니다.
- `on_error=continue`: 현재 단계가 실패해도 의존하지 않는 뒤 단계는 실행할 수 있습니다.
- 실패한 단계에 의존하는 단계는 `on_error=continue`여도 Tool을 호출하지 않고 `blocked`가 됩니다.
- step 형식, run ID, Loop 순서가 잘못된 구조 오류는 항상 전체 실행을 중단합니다.

## 질문 자리표시자

등록 Workflow의 question에서 다음 두 형태만 지원합니다.

```text
{{user_question}}
${user_question}
```

전체 사용자 질문을 모든 단계에 반복하지 마십시오. 날짜·제품·공정 등 원문의 해석이 필요한 첫 단계에만 사용하고, 후속 단계는 `이전 결과의 LOT`처럼 업무 목적을 명확히 적습니다.

## JSON Registry 형식

가장 권장하는 형식은 `workflows` object입니다.

```json
{
  "workflows": {
    "anomaly_lot_hold_history": {
      "title": "이상 LOT 및 HOLD 이력 분석",
      "steps": []
    },
    "production_wip_summary": {
      "title": "생산량 및 재공 요약",
      "steps": []
    }
  }
}
```

다음 형태도 지원합니다.

- 최상위 `workflow_key -> workflow object`
- `workflows` 배열 안의 `{workflow_key, steps}` object
- 최상위 배열 안의 `{workflow_key, steps}` object

같은 key가 둘 이상이면 모호한 등록으로 차단합니다. `workflow_key`가 지정되면 inline 정의보다 Registry를 우선하며, 미등록 key를 inline으로 우회하지 않습니다.

## MongoDB 저장 문서 형식

운영 Route V4는 `datagov.agent_v4_workflow_skills`에서 다음 형태의 활성 문서를 조회합니다.

```json
{
  "_id": "workflow:anomaly_lot_hold_history",
  "section": "workflow_skills",
  "key": "anomaly_lot_hold_history",
  "status": "active",
  "payload": {
    "display_name": "이상 LOT 및 HOLD 이력 분석",
    "description": "이상 LOT을 찾은 뒤 해당 LOT의 HOLD 이력을 분석합니다.",
    "aliases": ["이상 LOT HOLD 분석"],
    "intent_examples": ["오늘 이상 LOT을 분석하고 해당 LOT HOLD 이력을 알려줘"],
    "keywords": ["이상 LOT", "HOLD 이력"],
    "excluded_keywords": [],
    "priority": 100,
    "steps": []
  }
}
```

`00A Workflow Registry 로더`는 `key`와 `payload`의 허용 필드만 계획 모델에 전달합니다. exact key/alias를 먼저 선택하고, 나머지는 `keywords`, `intent_examples`, `description`, `display_name` 관련도로 정렬해 최대 8개만 전달합니다. `mongodb` 모드에서 연결 실패나 빈 조회가 발생해도 inline seed로 자동 대체하지 않습니다.

## Inline Markdown 형식

운영자가 짧게 확인할 때만 사용합니다. 복잡한 질문은 JSON을 권장합니다.

```markdown
workflow_key: anomaly_lot_hold_history
title: 이상 LOT 및 HOLD 이력 분석

### find_abnormal_lots
tool_name: run_data_analysis
question: {{user_question}} 기준으로 이상 LOT을 분석해줘.
depends_on: []
handoff: none
on_error: stop

### analyze_hold_history
tool_name: run_data_analysis
question: 이전 결과의 LOT HOLD 이력을 분석해줘.
depends_on: [find_abnormal_lots]
handoff: result_ref
on_error: stop
```

Markdown은 위 key-value 형식만 지원합니다. 임의 자연어 문장에서 실행 계획을 추측하지 않습니다.

## 등록 전 점검

1. 단계가 4개 이하인지 확인합니다.
2. `Allowed Tool Names`에 실제 Tool 이름을 등록해 parser 단계에서 오타를 차단합니다.
3. 모든 dependency가 앞 단계인지 확인합니다.
4. `result_ref`를 생성하지 않는 Tool을 handoff 원본으로 사용하지 않습니다.
5. `upstream_result_ref`를 받지 않는 Tool을 handoff 대상에 사용하지 않습니다.
6. 전체 데이터 행을 question이나 summary에 복사하지 않습니다.
7. 쓰기 Tool은 명시적 사용자 요청이 있을 때만 별도 Workflow에 등록합니다.
