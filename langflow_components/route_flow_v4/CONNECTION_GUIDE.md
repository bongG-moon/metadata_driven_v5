# 08 Workflow Orchestrator 연결 가이드

08은 07 단일 호출 Router와 별도로 제공되는 Workflow Orchestrator입니다. 단일 Flow를 빠르게 호출할 때는 07을 유지하고, 등록된 최대 4단계 연계 질문에만 08을 사용합니다.

## 권장 캔버스 구조

```text
Chat Input ───────────────> 00A Workflow Registry 로더 ──┬─> 계획 Prompt Template
       │                      (질문 관련 최대 8개)          └─> 00 Workflow 계획 파서
       └────────────────────────────────────────────────────> 계획 Prompt Template

계획 Prompt Template -> 기본 Language Model -> 00 Workflow 계획 파서
                    ^                                      │
                    └─ 후보 Registry/허용 Tool 목록        ├─ loop_dataframe
                                                           v
                                                     Langflow 기본 Loop
                                      Item │
                                           v
                              01 순차 Workflow 단계 실행기 <── Tool 목록
                                           │
                                           └──────────────> Looping

Loop.Done ──────────────────────────────> 02 최종 합성 Context 생성기
00.workflow_plan ───────────────────────> 02.execution_context
Chat Input ─────────────────────────────> 02.user_question

02.question ───────────────┐
02.workflow_context ───────┼─> Prompt Template → native Language Model
02.synthesis_instruction ──┘                         │
02.final_context ───────────────────────────────────┼─> 03 Workflow 최종 응답 생성기
                                                    │             ├─ message → Chat Output 1개
Language Model Message ─────────────────────────────┘             └─ api_response → terminal
```

## 포트 연결표

| 출발 포트 | 도착 포트 | 설명 |
| --- | --- | --- |
| `Chat Input.message` | `00A.user_question` | exact key/alias와 등록 문구 관련도를 계산할 현재 질문입니다. |
| `00A.workflow_registry_json` | 계획 Prompt의 `workflow_registry_json` | 전체 문서가 아닌 질문 관련 후보를 최대 8개·바이트 제한으로 전달합니다. |
| `00A.workflow_registry_json` | `00.workflow_registry_json` | 계획 모델과 결정론적 Parser가 정확히 같은 후보 정의를 사용합니다. |
| `Chat Input.message` | 계획 Prompt의 `user_question` | 자연어 번호 목록 또는 등록 key를 실행 계획으로 변환합니다. |
| 계획 Prompt | 계획용 기본 Language Model | `workflow.plan.v1` JSON 하나만 생성합니다. |
| 계획 모델 결과 | `00.workflow_input` | 결정론적 파서가 Tool·순서·handoff·4단계 제한을 검증합니다. |
| `Chat Input.message` | `00.user_question` | 등록 Workflow의 `{user_question}` 자리표시자와 최종 질문에 사용합니다. |
| `00.loop_dataframe` | `Loop.Inputs` | 기본 경로입니다. 설치 버전에서 Data 목록이 필요하면 `loop_data_list`를 사용합니다. |
| `Loop.Item` | `01.loop_item` | 한 번에 정확히 한 step만 전달합니다. |
| Workflow 연계 Tool들 | `01.tools` | `route_v3.tool_result.v1` 하위 호환 계약을 반환하는 Tool만 연결합니다. |
| `01.step_result` | `Loop.Looping` | compact 결과를 Loop에 반환해 다음 항목을 진행합니다. |
| `Loop.Done` | `02.loop_results` | 모든 step 결과를 마지막 합성 Context로 변환합니다. |
| `00.workflow_plan` | `02.execution_context` | 계획 검증 실패도 최종 오류 응답에 보존합니다. advanced 포트가 아닙니다. |
| `02`의 Message 3종 | `Prompt Template` 변수 3종 | 변수 이름은 `question`, `workflow_context`, `synthesis_instruction`입니다. |
| Prompt 결과 | native Language Model | Langflow 기본 모델 컴포넌트를 사용합니다. |
| `02.final_context` | `03.final_context` | 모델 오류와 별개로 결정론적 실행 상태를 유지합니다. |
| 모델 Message | `03.final_model_response` | 비어 있거나 명시 오류여도 `03`이 최종 상태를 만듭니다. |
| `03.message` | 단일 `Chat Output` | 부모 08만 최종 답변을 저장합니다. |
| `03.api_response` | terminal Data output | API 계약의 유일한 terminal 구조화 결과입니다. |

## 기본 Loop 설정

- `00.loop_dataframe`을 Loop의 `Inputs`에 연결합니다.
- Loop의 `Item`에는 `01` 하나만 직접 연결합니다.
- `01.step_result`를 Loop의 `Looping`에 되돌립니다.
- JSON import에서는 이 피드백 edge가 일반 입력 포트가 아니라 `item`의 loop 전용 target handle로 저장되며,
  Langflow 1.8.x 호환을 위해 허용 타입 `Data`, `Message`가 모두 포함되어야 합니다.
- Loop의 `Done`을 `02.loop_results`로 연결합니다.
- If-Else를 Loop 내부에 추가하지 않습니다. dependency와 `on_error`는 `01`이 결정론적으로 처리합니다.

## Tool 조건

- 08에는 기존 조회·저장 Tool 5개와 `run_visualization`을 합쳐 정확히 6개를 연결합니다. 07 단일 호출 Router의 Tool 5개에는 시각화 Flow를 추가하지 않습니다.
- Tool의 `name`은 registry의 `tool_name`과 대소문자까지 정확히 같아야 합니다.
- 같은 이름의 Tool을 둘 이상 연결하면 실행을 차단합니다.
- Tool은 `question`을 필수 입력으로 받고, 연계 Tool은 선택적으로 `upstream_result_ref`를 받아야 합니다.
- Tool 출력은 `route_v3.tool_result.v1`이어야 합니다. 자연어 Message에서 ref를 다시 추출하지 않습니다.
- 하위 Flow Chat Input/Output의 메시지 저장은 꺼 두고 부모 08의 Chat Output만 저장합니다.
- 계획 Prompt와 `00 Workflow 계획 파서`에는 이름 목록뿐 아니라 각 Tool의 설명과 `accepts_upstream_result_ref`·`can_produce_result_ref`·`requires_upstream_result_ref` capability catalog를 넣습니다.
- `run_visualization`은 첫 단계로 실행하지 않고, 단일 `run_data_analysis` 선행 단계의 `result_ref`를 입력으로 받습니다.
- `handoff`는 현재 단계가 이전 결과를 입력으로 소비하는지 나타냅니다. 따라서 producer인 첫 `run_data_analysis`는 `handoff=none`, consumer인 `run_visualization`은 `handoff=result_ref`입니다.
- 외부 모델이 이 두 값을 정확히 반대로 생성한 경우에도 capability상 producer와 필수 consumer가 각각 하나로 확정될 때만 파서가 위치를 교정합니다. 후보가 둘 이상이면 추측하지 않고 기존처럼 실행을 차단합니다.

## 하위 Flow 최종 출력 선택 규칙

`04 Workflow 이름 기반 Cached Run Flow 도구`는 특정 node ID나 `22 API 응답 생성기`에 의존하지 않습니다. 하위 Flow를 추가하거나 출력 이름을 바꿀 때는 각 Tool 노드의 **우선 최종 출력 이름**만 설정합니다.

선택 순서는 다음과 같습니다.

1. `우선 최종 출력 이름`에 입력한 이름을 앞에서부터 찾습니다. 쉼표·세미콜론·줄바꿈으로 여러 후보를 입력할 수 있습니다.
2. 설정을 비우면 후속 연결이 없는 terminal 중 유일한 `Data`·`DataFrame`·`Table` 출력을 자동 선택합니다.
3. 구조화 출력이 여러 개면 임의로 고르지 않고 후보 목록과 함께 설정 오류를 반환합니다.
4. 선택된 custom terminal은 하위 Flow를 실행하는 동안에만 `is_output=true`로 승격합니다. 따라서 하위 Flow의 Chat Output은 직접 실행 화면용으로 그대로 유지할 수 있습니다.

현재 v5 하위 Flow 6종은 모두 `api_response`를 구조화 terminal 이름으로 사용하므로 08 JSON에는 `api_response`가 명시돼 있습니다. 새 Flow의 terminal 이름이 `result`, `dataset_payload` 등이라면 Tool 노드에서 그 이름으로 바꾸면 됩니다.

하위 Flow 작성 시 주의점:

- 구조화 결과 포트가 있는 노드는 후속 edge가 없는 terminal이어야 합니다.
- 같은 component의 Message 포트를 Chat Output에 연결하면서 그 component 자체를 구조화 terminal로 쓰면 vertex가 terminal이 아니게 됩니다. 이 경우 구조화 결과만 받는 별도 종료 어댑터를 하나 둡니다.
- `결과 참조 생성 가능`을 켠 Tool은 선택한 terminal 타입이 `Data` 계열이어야 합니다.
- Flow를 복제하거나 다시 import해 ID가 바뀌어도 Tool은 현재 이름으로 실제 ID를 다시 해석합니다. 숨겨진 과거 ID를 재사용하지 않습니다.

예시:

| 하위 Flow terminal | `우선 최종 출력 이름` 설정 |
| --- | --- |
| `api_response: Data` | `api_response` |
| `dataset_payload: Data` | `dataset_payload` |
| `result: Data`, `audit: Data` 두 개 | 사용할 하나를 `result` 또는 `audit`로 명시 |
| `message: Message` 하나만 존재 | 설정을 비우면 단일 terminal로 사용 가능. 단, `result_ref` handoff는 지원하지 않음 |

## Context 수명과 격리

`01`은 Loop 반복 사이의 compact 결과를 `user_id + flow_id + session_id + workflow_run_id`로 격리한 메모리 registry에 잠시 보관합니다. TTL은 1시간이며 최대 256개 run만 유지합니다. 마지막 step 처리 후 즉시 제거하고, `02`는 `Loop.Done`의 step 결과 목록으로 최종 Context를 재구성합니다.

## Workflow Registry 소스

운영 기본값은 `mongodb`이며 다음 standalone 입력을 캔버스에서 직접 확인할 수 있어야 합니다.

| 입력 | 기본값/연결 |
| --- | --- |
| `registry_source` | `mongodb` |
| `mongo_uri` | Langflow Credential Global Variable `MONGO_URL` |
| `mongo_database` | `datagov` |
| `collection_name` | `agent_v4_workflow_skills` |
| `status_filter` | `active` |
| `candidate_limit` | `8` 이하 |
| `max_registry_bytes` | `65536` |

`00A`는 `section=workflow_skills` 문서에서 `key`, `status`, `payload`만 projection 조회하고, `payload`에서도 제목·설명·별칭·질문 예시·키워드·우선순위·최대 4단계만 허용합니다. 질문과 무관한 전체 Registry를 계획 모델에 전달하지 않습니다.

`inline_seed`는 import 직후 로컬 검증을 위한 **명시적 소스 모드**입니다. `mongodb` 조회가 실패하거나 결과가 비어도 inline seed로 자동 전환하지 않으며, `00A.registry_status`와 Registry의 `meta`에 `error` 또는 `empty` 상태를 남깁니다. 따라서 Workflow Skill 저장 Flow가 MongoDB에 문서를 저장하면 08의 다음 호출부터 새 문서를 다시 조회할 수 있습니다.

## 최종 API 계약

`03.api_response`는 다음 envelope을 반환합니다.

```json
{
  "response_type": "workflow_orchestration",
  "status": "ok",
  "message": "최종 사용자 답변",
  "workflow": {
    "contract_version": "workflow.final_context.v1",
    "workflow_run_id": "...",
    "workflow_key": "hold_lot_history_metadata_audit",
    "execution_status": "complete",
    "step_count": 3,
    "steps": []
  },
  "errors": []
}
```

API에는 `prompt_variables`, 전체 rows, 전체 trace, pandas 코드, 내부 `result_ref`를 포함하지 않습니다.
