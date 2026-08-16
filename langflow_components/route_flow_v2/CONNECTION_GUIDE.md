# Route Flow v2 연결 가이드 (7종 Agent Tool)

이 Flow는 기존 API Smart Router를 대체하지 않는 별도 대안입니다. Agent가 일곱 개 하위 Flow 도구 중 하나를 선택하고, 하위 Flow의 최종 답변을 단일 Chat Output으로 반환합니다.

## 구성

```text
Chat Input.message -> Agent.input_value
Cached Flow Tool 7종.component_as_tool -> Agent.tools
Agent.response -> Direct Tool Result Adapter.agent_message
Direct Tool Result Adapter.message -> Chat Output.input_value
```

- Chat Output은 정확히 하나입니다.
- Chat Input은 Agent에만 한 번 연결합니다. Tool 7개는 LFX Tool wrapper가 `_pre_run_setup()`을 건너뛰는 경로에서도 실행 직전에 부모 runtime/graph `session_id`를 확정하므로 별도 세션 Message edge가 없습니다.
- 각 Tool의 `cache_flow`와 `return_direct`는 `true`입니다.
- Router Agent의 `n_messages`는 `5`, `max_iterations`는 `1`입니다. 이 값은 현재 저장 메시지 1개와 이전 사용자/응답 2턴 4개를 조회합니다. Native Chat Input Message ID를 기준으로 현재 입력을 history에서 제거하므로, 모델에는 현재 질문이 중복되지 않은 이전 2턴만 전달됩니다. 선택된 Data Analysis Flow는 같은 부모 세션으로 저장된 분석 상태도 함께 복원합니다.
- `flow_name_selected`는 기본 Run Flow처럼 새로고침 가능한 Flow 선택 드롭다운입니다. 실제 환경에서는 하위 Flow를 import한 뒤 각 Tool 노드에서 목록을 새로고침하고 대상을 다시 선택하면 현재 Flow ID가 `flow_id_selected`에 저장됩니다.
- 기본 `Flow 해석 방식=Flow ID 우선`은 선택된 실제 ID를 먼저 확인합니다. 해당 ID가 현재 프로젝트에 있으면 최신 이름과 `updated_at`으로 실행하고, Flow 재import 등으로 ID가 사라졌을 때만 저장된 이름으로 현재 Flow를 다시 찾습니다.
- 이름 검색을 완전히 금지하려면 각 Tool 노드에서 `Flow 해석 방식=선택한 Flow ID만`을 선택합니다. 이 모드에서 ID가 비어 있으면 잘못된 Flow를 실행하지 않고 대상 Flow 재선택 안내 오류를 반환합니다.
- `flow_id_selected`는 환경마다 달라지므로 repository export에서는 계속 비워 둡니다. 실제 환경에서 드롭다운을 다시 선택해 현재 ID를 저장해야 합니다.
- 같은 이름의 이전 Flow와 새 Flow가 프로젝트에 동시에 남아 있으면 어느 쪽이 최신 운영 대상인지 이름만으로 판단할 수 없습니다. 이 경우에는 이전 Flow를 정리하거나 Tool 노드에서 현재 Flow를 명시적으로 다시 선택해야 합니다.
- 이름/ID 조회와 그래프 캐시는 Langflow가 component에 주입한 현재 실행 `user_id`를 사용합니다. `_user_id`가 없을 때는 Langflow 기본 속성이 부모 `graph.user_id`를 사용하며, custom component가 이 읽기 전용 값을 덮어쓰지 않습니다. Router와 하위 Flow 7종은 반드시 같은 사용자로 import하고 같은 사용자 또는 API key로 실행해야 합니다.
- 선택된 ID는 실행할 때마다 현재 Flow metadata로 확인합니다. 그래프 캐시는 실제 `user_id + flow_id` 키를 사용하며, 대상 Flow의 현재 `updated_at`이 바뀌면 이전 그래프를 사용하지 않고 다시 구성합니다.
- 캐시는 Flow 그래프 구성 비용만 줄입니다. 데이터 조회, pandas 실행, LLM 답변 결과는 매 요청 다시 실행합니다.
- Flow를 선택하거나 목록을 새로고침해도 이 노드의 외부 출력은 `component_as_tool` 하나로 유지됩니다. 하위 Flow의 native `message` 출력만 Tool 내부 실행 결과로 사용하며 Router canvas 포트로 노출되지 않습니다.
- `return_direct=true`로 Tool 실행 뒤 결과를 다시 LLM으로 재작성하지 않습니다.
- 하위 Flow에 화면용 `Chat Output.message`와 API용 구조화 terminal이 함께 있어도 Route V2는 현재 Chat Output만 실행 출력으로 활성화합니다. 다른 terminal은 해당 child 실행에서만 비활성화하므로 `return_direct=true` 결과가 하나의 Message로 끝납니다.

## Tool 매핑

| Tool | 대상 Flow |
| --- | --- |
| `run_data_analysis` | `01. v5_data_analysis` |
| `run_report_followup` | `10. v5_report_followup` |
| `run_metadata_qa` | `05. v5_metadata_qa` |
| `save_domain_metadata` | `02. v5_domain_saving` |
| `save_table_catalog_metadata` | `03. v5_table_catalog_saving` |
| `save_main_flow_filter_metadata` | `04. v5_main_flow_filter_saving` |
| `run_realtime_production_report` | `07. v5_realtime_production_report` |

## Report 후속 질문 분리

- 직전 응답이 같은 세션의 Report이고 저장 Snapshot 또는 Report가 미리 만든 집계 View의 컬럼 선택·필터·정렬·상위/하위 N을 요청하면 `run_report_followup`을 선택합니다. Flow 10 자체는 새 groupby 집계를 만들지 않습니다.
- `현재 기준`, `현재 데이터`, `지금 시점`, `최신 데이터`, `다시 조회`, `새로 조회`처럼 새 기준시점을 명시하거나 다른 데이터셋과의 결합이 필요하면 `run_data_analysis`를 선택합니다. `현재작업재공`처럼 `현재`가 Report 컬럼명의 일부인 경우는 재조회 신호가 아닙니다.
- Agent는 Report의 `context_ref`나 원천 행을 Tool 인자로 전달하지 않습니다. Flow 10이 같은 세션 상태에서 참조를 복원하고 만료·완전성·허용 연산을 검증합니다.
- Flow 10 오류를 Flow 01로 자동 fallback하지 않습니다. 이 규칙은 Snapshot 질문이 모델 판단 하나로 원천 재조회되는 것을 막습니다.

## 실시간 생산 분석 실행 Gate

`run_realtime_production_report`는 다음 두 조건을 모두 만족할 때만 07번 Flow를 실행합니다.

1. 질문에 `분석`이 포함되어야 합니다.
2. `실시간 생산 분석`, `실시간 분석`, `실시간 생산분석` 중 하나가 포함되어야 합니다.

예를 들어 `W/B 공정그룹 실시간 생산 분석을 해줘`는 실행되고, `W/B 실시간 생산 현황을 보여줘`는 실행되지 않습니다. Agent Prompt가 이 우선순위를 판단하며, Cached Flow Tool의 `필수 키워드`와 `허용 호출 구문` 입력이 실제 실행 직전에 같은 조건을 다시 검사합니다.

## Tool schema 절감과 안정화

표준 Run Flow Tool은 Data Analysis Flow의 편집 가능한 Text Input 프롬프트까지 Agent 인자로 노출할 수 있습니다. 이 구현의 7개 노드는 필수 `question` 한 필드만 공개합니다. 내부 지시문, helper 코드, repair prompt는 기존 canvas에서 계속 편집할 수 있지만 Router Agent 토큰에는 포함되지 않습니다.

외부 Tool 인자는 항상 `flow_tweak_data.question`입니다. 실행 직전에 현재 그래프의 단일 Chat Input ID를 찾아 내부 `ChatInput-...~input_value` tweak로 변환합니다. 따라서 standalone import가 node ID를 다시 발급하거나 모델/provider가 Tool 필드명의 특수문자를 정규화하더라도 질문 필드가 바뀌지 않습니다.

## 성능 특성

- 첫 실행: Agent의 Tool 선택 + 하위 Flow 그래프 로드 + 하위 Flow 실행
- 이후 실행: Agent의 Tool 선택 + 캐시된 그래프 복원 + 하위 Flow 실행
- `return_direct=true`: Tool 실행 뒤 결과를 다시 쓰는 추가 Agent LLM 단계를 생략

분류만 필요한 운영 기본 경로에서는 API Smart Router가 더 빠를 수 있습니다. Agent Tool Router는 복합적인 자연어 분류 완성도와 관리 편의성을 비교 검증하기 위한 대안입니다.
