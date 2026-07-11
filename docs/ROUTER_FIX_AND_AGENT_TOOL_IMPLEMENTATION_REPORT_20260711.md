# Router 중복 출력 수정 및 Agent Tool Router 구현 보고서

- 기준 환경: Langflow 1.8.2 / LFX 0.3.4 standalone
- 구현일: 2026-07-11
- 운영 데이터 계약: 기존 `datagov.agent_v4_*` collection 유지

## 결론

기존 API Router의 중복 출력은 두 단계로 확인했습니다. `질문 JSON` 두 개는 blind FinalGate가 만든 것이어서 제거했지만, 이후 질문 Message 두 개가 다시 나타났습니다. 실행 DB를 확인하니 하위 API는 한 번만 호출됐고, Chat Input을 Smart Router와 API caller 5개에 동시에 연결한 fan-out 때문에 ChatInput/SmartRouter가 각각 두 번 빌드되며 비선택 direct/clarification Chat Output이 질문을 저장했습니다. 최종 구조는 예전 정상 Flow처럼 Chat Input을 Smart Router에만 연결합니다.

운영 기본 API Router는 그대로 유지하면서, 비교 가능한 별도 `Agent + Tool Mode Router`도 추가했습니다. 두 Router는 각각 독립 endpoint를 갖습니다.

## API Router 변경

```text
변경 전: SmartRouter -> FinalGate 2개 -> ChatOutput 2개
변경 후: SmartRouter -> ChatOutput 2개
```

- API caller 5개 유지
- 각 caller의 240초 read timeout 유지
- `Chat Input.message -> API caller.session_source` 5개 edge 제거
- Chat Input outgoing edge를 Smart Router 한 개로 제한하고 caller session은 Langflow 자동 주입 사용
- 선택된 API branch 호출 횟수 1회 계약 유지
- API Router: 16 nodes / 20 edges에서 14 nodes / 13 edges로 축소

## Agent + Tool Mode Router

```text
Chat Input.message -> Agent.input_value
Cached Flow Tool 5종.component_as_tool -> Agent.tools
Agent.response -> Chat Output 1개
```

Tool의 별도 `session_source` 입력과 Chat Input fan-out 5개는 제거했습니다. 질문은 Agent가 Tool 인자로 전달하고, 세션은 각 Tool이 부모 `graph.session_id`에서 자동 상속합니다.

Tool은 아래 다섯 개입니다.

- `run_data_analysis`
- `run_metadata_qa`
- `save_domain_metadata`
- `save_table_catalog_metadata`
- `save_main_flow_filter_metadata`

Agent는 `max_iterations=3`, `n_messages=6`, `verbose=false`, `add_current_date_tool=false`입니다. 한 요청에서 정확히 하나의 Tool만 호출하고, 모호한 요청은 Tool 없이 확인 질문 한 번만 하도록 system prompt를 구성했습니다.

## standalone import와 cache 처리

Langflow upload는 export JSON의 Flow ID를 그대로 보존하지 않고 새 DB ID를 발급합니다. 따라서 표준 Run Flow에 export 시점 ID를 넣으면 다른 환경에서 깨질 수 있습니다.

새 `CachedNamedRunFlowTool`은 다음 순서로 동작합니다.

1. 고정 ID 대신 정확한 Flow 이름을 저장합니다.
2. 실행 시 같은 프로젝트 DB에서 현재 Flow ID와 `updated_at`을 조회합니다.
3. 실제 `user_id + flow_id`로 Langflow shared graph cache를 사용합니다.
4. 대상 Flow가 갱신되면 `updated_at` 비교로 오래된 graph cache를 무효화합니다.

`cache_flow=true`는 그래프 파싱·구성 비용만 줄입니다. 데이터 조회, pandas, 하위 LLM 답변 결과는 매 요청 다시 실행합니다. 따라서 warm 실행에서 일부 단축을 기대할 수 있지만 API Smart Router보다 항상 빠르다는 의미는 아닙니다.

## Tool payload/token 절감

표준 Run Flow Tool은 Data Analysis의 편집용 Intent/Pandas/Answer/Repair Text Input까지 Agent-controlled schema로 노출했습니다.

- 표준 필드: 5개, schema 26,338 bytes
- 개선 필드: `ChatInput.input_value` 1개, schema 356 bytes

내부 prompt, helper, repair prompt는 Data Analysis canvas에서 계속 편집할 수 있지만 Router Agent 요청에는 포함되지 않습니다. Tool 5개는 `return_direct=true`이므로 하위 Flow가 만든 최종 답변을 다시 쓰기 위한 추가 Agent LLM 단계도 생략합니다.

## 산출물

- 단일 7-Flow import: `import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`
- 수정된 API Router: `import_ready_flows/06_api_router_flow_v5_standalone.json`
- 신규 Agent Tool Router: `import_ready_flows/07_agent_tool_router_flow_v5_standalone.json`
- 전체 ZIP: `import_ready_flows.zip`
- 06 API Router source: `langflow_components/route_flow/01_flow_api_message_caller.py`
- 07 Tool component: `langflow_components/route_flow_v2/01_cached_named_run_flow_tool.py`
- 07 Agent system prompt: `langflow_components/route_flow_v2/SYSTEM_PROMPT_KO.md`

## 검증 결과

- pytest: 222 passed
- 대표 Data Analysis dummy 질문: 23/23 passed
- frontend edge handle: 286/286 parse 및 `edge.data` 일치
- LFX node template: 115/115 passed
- 격리 Langflow import: 7/7 HTTP 201
- Agent Tool Router partial build: 새로 발급된 Data Analysis ID를 이름으로 해석하고 `CachedFlowTool-data_analysis`까지 성공
- API Router: FinalGate 0개, 직접 terminal edge 2/2
- Agent Tool Router: Tool 5개, cache 5/5, return direct 5/5, Chat Output 1개

격리 서버에는 provider Global Variable이 없어 Agent가 실제로 Tool을 선택하는 LLM E2E는 수행하지 않았습니다. 운영 환경에서는 동일 질문을 API Router와 Agent Tool Router에 각각 5회 호출해 첫 실행과 warm 실행의 P50/P95를 비교해야 합니다.

## 적용 방법

기존 하위 Flow 5개가 현재 bundle 이름으로 이미 import되어 있다면 기존 API Router만 정리한 뒤 `06`과 신규 `07`만 import하면 됩니다. 전체를 다시 가져올 때는 이름에 `(1)`이 붙지 않도록 기존 동일 bundle Flow를 먼저 정리하고 단일 `00` 파일을 import합니다.
