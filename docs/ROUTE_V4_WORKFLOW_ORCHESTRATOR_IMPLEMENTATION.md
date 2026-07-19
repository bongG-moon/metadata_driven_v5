# 08 문서 기반 순차 Workflow Orchestrator

## 목적

08 Workflow Orchestrator는 사용자가 자연어 또는 사전에 등록한 업무 문서로 실행 순서를 명시할 때, 최대 네 개의 하위 Flow를 한 번에 하나씩 순차 실행하고 마지막에만 결과를 종합한다.

```text
업무 정의
  -> 기본 Language Model 계획 생성
  -> 결정론적 계획 검증
  -> 기본 Langflow Loop 순차 실행
  -> 단계 결과 축약
  -> 기본 Language Model 최종 합성 1회
  -> 단일 Chat Output / API 응답
```

08은 실행 전에 전체 계획을 확정하고 Loop가 그 계획을 그대로 수행하므로 반복 업무, 승인된 업무 절차와 재현 가능한 실행에 적합하다.

## 07·08 선택 기준

| Router | 실행 방식 | 적합한 질문 |
| --- | --- | --- |
| 07 Agent Tool Router | Agent가 Tool 하나 선택, 직접 반환 | 단일 분석 또는 단일 메타데이터 조회 |
| 08 Workflow Orchestrator | 계획을 먼저 고정하고 Loop로 순차 실행 | 정해진 업무 순서, 재사용 Workflow, 감사 가능한 반복 업무 |

단순 질문은 07이 가장 작고 빠르다. 08은 계획 생성과 최종 합성으로 Language Model을 두 번 사용하므로, 순차 업무라는 요구가 있을 때 사용한다.

## Flow 구성

```text
Chat Input
  -> 00A Workflow Registry 로더 -> 계획 Prompt / 00 Workflow 계획 파서
  -> 계획 Prompt Template
  -> 기본 Language Model
  -> 00 Workflow 계획 파서
  -> 기본 Loop.data

기본 Loop.item
  -> 01 순차 단계 실행기 <- 이름 기반 Cached Flow Tool 6개
  -> 기본 Loop.looping

기본 Loop.done
  -> 02 최종 컨텍스트 구성기
  -> 기본 Language Model
  -> 최종 응답 구성기
  -> Chat Output
  -> api_response
```

- 계획 생성과 최종 합성은 Langflow 기본 `Language Model`을 사용한다.
- 반복 제어는 Langflow 기본 `Loop`를 사용한다.
- 별도 custom Agent를 만들지 않는다.
- `01 순차 단계 실행기`는 계획에 지정된 Tool 하나만 직접 호출하며 Tool을 선택하지 않는다.
- child graph는 선택된 Tool을 실행할 때만 이름으로 해석하고 캐시한다.
- 등록 Workflow와 일치하지 않는 요청도 여섯 Tool의 capability catalog만으로 해결 가능하면 `workflow_key=inline` 계획을 만든다.
- 그래프·차트 요청은 `run_data_analysis` 다음에 `run_visualization`을 두고 `handoff=result_ref`로 실제 결과를 전달한다.
- 부모 Chat Input/Output만 메시지를 저장한다.
- 하위 Flow 출력은 고정 node ID가 아니라 `우선 최종 출력 이름`으로 선택한다. 설정을 비우면 유일한 구조화 terminal을 자동 선택한다. 현재 하위 Flow의 terminal은 Python component가 `self.is_output = True`를 선언하며, wrapper의 실행 시점 승격은 이 선언이 없는 구버전 Flow를 위한 호환 장치다.

## 계획 계약

계획 모델은 다음 `workflow.plan.v1` JSON만 반환한다. 파서는 JSON 형식, 1~4단계, 고유 step ID, 등록 Tool, 앞 단계만 참조하는 의존성, handoff와 오류 정책을 검증한다. 검증 실패 시 Tool은 한 번도 호출되지 않는다.

```json
{
  "contract_version": "workflow.plan.v1",
  "workflow_key": "daily_manufacturing_briefing",
  "title": "DA 일일 생산·재공 브리핑",
  "description": "DA 생산량과 재공 조회 후 사용 데이터셋 정의 확인",
  "steps": [
    {
      "step_id": "production",
      "tool_name": "run_data_analysis",
      "question": "오늘 D/A 공정 그룹의 세부 공정별 생산량을 조회해.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    },
    {
      "step_id": "metadata",
      "tool_name": "run_metadata_qa",
      "question": "메타데이터에 등록된 WB 공정 그룹 정의와 포함 공정을 알려줘.",
      "depends_on": ["production"],
      "handoff": "none",
      "on_error": "continue"
    }
  ]
}
```

등록 Workflow를 사용할 때도 MongoDB 내용을 그대로 실행하지 않고 같은 파서 검증을 통과해야 한다. `00A Workflow Registry 로더`는 `datagov.agent_v4_workflow_skills`의 active 문서를 현재 질문 기준 최대 8개·64KB 후보로 줄여 계획 Prompt와 파서에 동일하게 전달한다. `inline_seed`는 명시적 테스트 모드이며 MongoDB 실패 시 자동 fallback하지 않는다.

## 순차 실행 보장

Loop는 계획의 단계 목록을 순서대로 한 항목씩 내보낸다. 단계 실행 결과가 Loop의 feedback 포트로 돌아온 뒤에만 다음 항목이 실행된다. 따라서 여러 Tool이 executor에 연결되어 있어도 같은 iteration에서 선택된 `tool_name`과 정확히 일치하는 Tool 하나만 호출된다.

Loop 반복 사이에는 `workflow_run_id`로 격리된 축약 실행 상태만 유지한다.

```json
{
  "contract_version": "workflow.execution.v1",
  "workflow_run_id": "workflow:...",
  "execution_order": ["production", "metadata"],
  "results_by_step": {
    "production": {
      "status": "ok",
      "summary": "오늘 WB 생산량은 ...입니다.",
      "result_ref": "result:session-id:uuid",
      "entity_ids": []
    }
  }
}
```

원본 rows, SQL, pandas code, trace는 Loop 상태나 최종 모델 prompt에 복제하지 않는다. 각 단계는 Tool의 compact 결과에서 `status`, `summary`, `result_ref`, `entity_ids`, 제한된 warning/error만 남긴다.

## 의존성과 데이터 전달

- `depends_on`: 실행 순서와 성공 조건을 표현한다.
- `handoff=none`: 선행 단계가 끝난 뒤 실행하지만 데이터는 넘기지 않는다.
- `handoff=result_ref`: 정확히 하나의 선행 단계가 만든 MongoDB Result Store 참조를 다음 Tool에 전달한다.

`result_ref`를 사용하는 단계는 선행 결과가 `status=ok`, `handoff_usable=true`이고 실제 ref가 있을 때만 실행한다. 요약문이나 entity preview를 전체 결과 대신 사용하는 fallback은 두지 않는다.

## 다른 하위 Flow 추가 방법

1. 하위 Flow에 후속 edge가 없는 구조화 terminal을 하나 둔다. 출력 타입은 `Data` 계열을 권장하며, component Python의 `__init__`에서 `super()` 호출 뒤 `self.is_output = True`를 선언한다. JSON은 직접 수정하지 않는다.
2. 08의 `04 Workflow 이름 기반 Cached Run Flow 도구`를 복제하고 `대상 Flow 이름`, `도구 이름`, `도구 설명`을 입력한다.
3. 하위 terminal의 포트 이름을 `우선 최종 출력 이름`에 입력한다. 현재 v5는 `api_response`를 사용하지만 이름 자체는 자유롭다.
4. 이전 결과를 입력받는 Flow만 `이전 결과 참조 입력 지원`을 켠다.
5. 성공 결과가 MongoDB Result Store 참조를 제공하는 Flow만 `결과 참조 생성 가능`을 켠다.
6. 새 Tool 이름을 Workflow 계획 파서의 허용 Tool/capability catalog와 Registry 문서에 추가한다.

Tool은 실행할 때마다 현재 사용자 범위에서 Flow 이름을 실제 ID로 다시 해석합니다. 따라서 Flow를 재import해 ID가 바뀌어도 JSON의 숨겨진 과거 ID를 따라가지 않습니다. 동일 이름 Flow가 여러 개 생기지 않도록 기존 Flow를 정리한 뒤 import해야 합니다.

## 오류 정책

- `on_error=stop`: 해당 단계 실패 후 나머지 모든 단계를 호출하지 않는다.
- `on_error=continue`: 해당 실패에 의존하는 단계는 건너뛰되, 의존하지 않는 다음 단계는 실행할 수 있다.
- 없는 Tool, 미래 단계 dependency, 중복 step ID, 4단계 초과는 계획 검증에서 차단한다.
- 모델이 병렬 호출을 제안해도 Loop 실행 계약에는 병렬 경로가 없다.
- 최종 답변은 성공·실패·건너뜀 단계를 구분하고 실패를 성공으로 바꾸지 않는다.

## Workflow 문서 관리

작성 예시는 `docs/workflows/`에 있다.

- `README.md`: 작성 규칙과 등록 절차
- `workflow_registry.example.json`: inline seed와 입력 작성용 registry 기준본
- `daily_manufacturing_briefing.md`: 생산·재공·Metadata QA 순차 실행 예시
- `hold_lot_history_metadata_audit.md`: 실제 `result_ref` handoff가 필요한 예시
- `equipment_uph_source_audit.md`: 장비·UPH 결과와 데이터 소스 감사 예시

문서는 감사와 변경 검토의 기준이며, 런타임은 Workflow Skill 저장 Flow가 MongoDB에 저장한 정의를 읽는다. 외부 문서를 런타임에 읽게 하지 않아 Kubernetes와 Desktop에서 같은 import 파일로 동작한다.

## 테스트 절차

1. 하위 Flow 1~5, 08 Workflow Orchestrator, 09 Workflow Skill 저장 Flow, 10 HTML Visualization Flow를 같은 Langflow 사용자로 import한다.
2. 두 Flow의 `MONGO_URL`, `datagov`, `agent_v4_workflow_skills` 입력과 모델 입력을 운영 환경에 맞게 설정한다.
3. 08 Workflow Orchestrator의 Tool 대상 이름이 실제 import 이름과 정확히 같은지 확인한다.
4. 저장 Flow에서 dry-run 확인 후 실제 저장하고 key-only 등록 Workflow를 실행한다.
5. 같은 업무를 자연어 번호 목록으로 실행한다.
6. 의도적으로 잘못된 Tool과 5단계 계획이 실행 전에 차단되는지 확인한다.
7. `result_ref` 예시는 같은 session에서 Result Store 저장·복원이 되는지 확인한다.
8. Playground 메시지가 질문 한 건과 최종 답변 한 건만 남는지 확인한다.
9. `최근 3일 D/A 공정 생산량을 조회하고 그래프로 그려줘`가 inline 계획의 Data Analysis → HTML Visualization 순서로 실행되는지 확인한다.

대표 입력:

```text
아래 업무를 순서대로 진행해.
1. 오늘 WB 공정 생산량을 조회해.
2. 1번 실행이 완료된 뒤 등록된 WB 공정 그룹 정의를 조회해.
3. 각 단계에서 한 번에 하나의 Tool만 호출하고 병렬로 호출하지 마.
4. 모든 실행이 끝난 뒤 결과를 하나의 답변으로 정리해줘.
```

예상 실행 순서:

```text
run_data_analysis
-> run_metadata_qa
-> 최종 Language Model 1회
```
