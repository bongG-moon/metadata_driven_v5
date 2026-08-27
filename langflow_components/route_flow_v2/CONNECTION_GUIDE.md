# Route Flow v2 연결 가이드 (6종 Agent Tool)

이 Flow는 기존 API Smart Router를 대체하지 않는 별도 대안입니다. Agent가 여섯 개 하위 Flow 도구 중 하나를 선택하고, 하위 Flow의 최종 답변을 단일 Chat Output으로 반환합니다.

## 구성

```text
GaiA Input.message -> GaiA 외부 세션 ID 추출.input_message
GaiA Input.message -> Router Session Context Loader.input_message
GaiA 외부 세션 ID 추출.external_session_id -> Router Session Context Loader.session_id
Router Session Context Loader.message -> Agent.input_value
Router Session Context Loader.canonical_session_id -> Cached Flow Tool 6종.session_id
Cached Flow Tool 6종.component_as_tool -> Agent.tools
Agent.response -> Direct Tool Result Adapter.agent_message
Router Session Context Loader.message -> Router Session State Writer.context_message
Agent.response -> Router Session State Writer.agent_message
Direct Tool Result Adapter.message -> Router Session State Writer.answer_message
Router Session State Writer.message -> Chat Output.input_value
```

- Flow 06은 운영 GaiA Input을 시작점으로 사용하고, native Chat Input은 두지 않습니다. GaiA Input은 외부 A2A의 `input_value`, `data`, `metadata`를 직접 받아 Message로 만들며, 세션 전용 추출 컴포넌트가 `metadata.session_id`만 별도 Message로 만듭니다.
- GaiA Input의 화면 표시명과 embedded source는 `GaiA Input`으로 유지하지만, 생성된 Flow vertex는 `ChatInput` interface type으로 표시됩니다. Langflow 1.11이 외부 `/run`의 top-level `input_value`를 이 interface type에만 자동 전달하기 때문이며, native Chat Input 노드를 추가했다는 뜻은 아닙니다.
- Router Session Context Loader는 `datagov.router_session_states`에서 **직전 사용자 질문, 직전 최종 답변, 직전 선택 Flow** 한 세트만 읽습니다. 이 컬렉션은 Data Analysis의 `agent_v4_session_states`와 별도입니다. 전체 대화 transcript는 Router에 전달하지 않습니다.
- Tool 6개는 Context Loader가 확정한 canonical 세션 ID를 직접 받습니다. 따라서 외부 Gateway가 매 요청마다 새 Langflow graph를 만들더라도 선택된 하위 Flow가 같은 세션의 분석 상태를 복원할 수 있습니다.
- Router Session State Writer는 직접 Tool 오류 없이 완료된 Tool 선택 뒤에만 직전 질문·최종 답변·선택 Flow를 저장합니다. 하위 Flow의 정상 clarification/Blocked 응답은 현재 Router 선택 문맥으로 유지하되, Tool 실행 오류는 이전 상태를 덮어쓰지 않습니다. MongoDB 연결·조회·저장이 실패하거나 세션 ID가 없으면 현재 질문은 기존 단일턴 경로로 그대로 실행됩니다.
- 각 Tool의 `cache_flow`와 `return_direct`는 `true`입니다.
- Router Agent의 `n_messages`는 `1`, `max_iterations`는 `1`입니다. Context Loader가 제공하는 직전 질문·답변·선택 Flow만 system prompt의 보조 문맥으로 사용하고, native Chat history는 Router 판단에 섞지 않습니다. 현재 질문이 완결된 새 요청이면 이 보조 문맥을 무시하고 현재 질문 기준으로 라우팅합니다. 선택된 Data Analysis Flow는 같은 canonical 세션으로 저장된 분석 상태를 자체적으로 복원합니다.
- `flow_name_selected`는 기본 Run Flow처럼 새로고침 가능한 Flow 선택 드롭다운입니다. 실제 환경에서는 하위 Flow를 import한 뒤 각 Tool 노드에서 목록을 새로고침하고 대상을 다시 선택하면 현재 Flow ID가 `flow_id_selected`에 저장됩니다.
- 기본 `Flow 해석 방식=Flow ID 우선`은 선택된 실제 ID를 먼저 확인합니다. 해당 ID가 현재 프로젝트에 있으면 최신 이름과 `updated_at`으로 실행하고, Flow 재import 등으로 ID가 사라졌을 때만 저장된 이름으로 현재 Flow를 다시 찾습니다.
- 이름 검색을 완전히 금지하려면 각 Tool 노드에서 `Flow 해석 방식=선택한 Flow ID만`을 선택합니다. 이 모드에서 ID가 비어 있으면 잘못된 Flow를 실행하지 않고 대상 Flow 재선택 안내 오류를 반환합니다.
- `flow_id_selected`는 환경마다 달라지므로 repository export에서는 계속 비워 둡니다. 실제 환경에서 드롭다운을 다시 선택해 현재 ID를 저장해야 합니다.
- 같은 이름의 이전 Flow와 새 Flow가 프로젝트에 동시에 남아 있으면 어느 쪽이 최신 운영 대상인지 이름만으로 판단할 수 없습니다. 이 경우에는 이전 Flow를 정리하거나 Tool 노드에서 현재 Flow를 명시적으로 다시 선택해야 합니다.
- 이름/ID 조회와 그래프 캐시는 Langflow가 component에 주입한 현재 실행 `user_id`를 사용합니다. `_user_id`가 없을 때는 Langflow 기본 속성이 부모 `graph.user_id`를 사용하며, custom component가 이 읽기 전용 값을 덮어쓰지 않습니다. Router와 여섯 개 하위 Tool 대상 Flow는 반드시 같은 사용자로 import하고 같은 사용자 또는 API key로 실행해야 합니다.
- 선택된 ID는 실행할 때마다 현재 Flow metadata로 확인합니다. 그래프 캐시는 실제 `user_id + flow_id` 키를 사용하며, 대상 Flow의 현재 `updated_at`이 바뀌면 이전 그래프를 사용하지 않고 다시 구성합니다.
- 캐시는 Flow 그래프 구성 비용만 줄입니다. 데이터 조회, pandas 실행, LLM 답변 결과는 매 요청 다시 실행합니다.
- Flow를 선택하거나 목록을 새로고침해도 이 노드의 외부 출력은 `component_as_tool` 하나로 유지됩니다. 하위 Flow의 native `message` 출력만 Tool 내부 실행 결과로 사용하며 Router canvas 포트로 노출되지 않습니다.
- `return_direct=true`로 Tool 실행 뒤 결과를 다시 LLM으로 재작성하지 않습니다.
- 하위 Flow에 화면용 `Chat Output.message`와 API용 구조화 terminal이 함께 있어도 Route V2는 현재 Chat Output만 실행 출력으로 활성화합니다. 다른 terminal은 해당 child 실행에서만 비활성화하므로 `return_direct=true` 결과가 하나의 Message로 끝납니다.

## Tool 매핑

| Tool | 대상 Flow |
| --- | --- |
| `run_data_analysis` | `01. v5_data_analysis` |
| `run_metadata_qa` | `05. v5_metadata_qa` |
| `save_domain_metadata` | `02. v5_domain_saving` |
| `save_table_catalog_metadata` | `03. v5_table_catalog_saving` |
| `save_main_flow_filter_metadata` | `04. v5_main_flow_filter_saving` |
| `run_realtime_production_report` | `07-1. v5_realtime_production_report` |

## 후속 질문의 일반 처리

- Router는 직전 사용자 질문·최종 답변·선택 Flow 한 세트를 **도구 선택 보조 정보**로만 사용합니다. 현재 질문이 생략형이고 그 문맥으로 대상·의도를 하나로 확정할 수 있을 때만 제조 데이터 후속 질문으로 판단합니다.
- 제조 데이터의 후속 조회·정렬·집계·계산은 `run_data_analysis`를 선택합니다. 조건 상속, 분석 상태 복원, 결과 재계산은 Data Analysis Flow가 같은 canonical 세션에서 처리합니다.
- 현재 질문이 단독으로 완결되었거나 새 대상·조건·지표·시점을 명시하면 Router는 직전 문맥을 적용하지 않습니다. 대상이 모호하면 임의로 이어 붙이지 않고 한 번만 구체적으로 확인합니다.
- `07-2. v5_report_followup`은 standalone/development artifact로 보존하지만, 현재 Flow 06의 Tool 목록에는 노출하지 않습니다. Report Snapshot 후속 분석을 운영 경로로 다시 채택하기 전까지 Router가 이 Flow를 선택하지 않습니다.

## 실시간 생산 분석 실행 Gate

`run_realtime_production_report`는 다음 두 조건을 모두 만족할 때만 07-1번 Flow를 실행합니다.

1. 질문에 `분석`이 포함되어야 합니다.
2. `실시간 생산 분석`, `실시간 분석`, `실시간 생산분석` 중 하나가 포함되어야 합니다.

예를 들어 `W/B 공정그룹 실시간 생산 분석을 해줘`는 실행되고, `W/B 실시간 생산 현황을 보여줘`는 실행되지 않습니다. Agent Prompt가 이 우선순위를 판단하며, Cached Flow Tool의 `필수 키워드`와 `허용 호출 구문` 입력이 실제 실행 직전에 같은 조건을 다시 검사합니다.

## Tool schema 절감과 안정화

표준 Run Flow Tool은 Data Analysis Flow의 편집 가능한 Text Input 프롬프트까지 Agent 인자로 노출할 수 있습니다. 이 구현의 6개 노드는 필수 `question` 한 필드만 공개합니다. 내부 지시문, helper 코드, repair prompt는 기존 canvas에서 계속 편집할 수 있지만 Router Agent 토큰에는 포함되지 않습니다.

외부 Tool 인자는 항상 `flow_tweak_data.question`입니다. 실행 직전에 현재 그래프의 단일 Chat Input ID를 찾아 내부 `ChatInput-...~input_value` tweak로 변환합니다. 따라서 standalone import가 node ID를 다시 발급하거나 모델/provider가 Tool 필드명의 특수문자를 정규화하더라도 질문 필드가 바뀌지 않습니다.

## 성능 특성

- 첫 실행: Agent의 Tool 선택 + 하위 Flow 그래프 로드 + 하위 Flow 실행
- 이후 실행: Agent의 Tool 선택 + 캐시된 그래프 복원 + 하위 Flow 실행
- `return_direct=true`: Tool 실행 뒤 결과를 다시 쓰는 추가 Agent LLM 단계를 생략
- Router Agent의 `handle_parsing_errors`는 `false`입니다. Langflow 1.11에서 이 입력은 `ToolRetryMiddleware(max_retries=2, retry_on=Exception)`를 추가하므로, 저장 Flow까지 포함한 상위 Router에서는 사용하지 않습니다.
- Tool 입력 검증 또는 실행 예외는 하위 Flow를 재실행하지 않고 `status=error`인 안전한 안내로 한 번 반환합니다. 정상적으로 반환된 Blocked·clarification 답변은 그대로 전달합니다.
- 저장 요청이 오류로 끝났다면 자동 재시도하지 말고 MongoDB 반영 상태를 먼저 확인합니다.

## 외부 GAIA/CUBE 세션 연결

Router의 native Message history만으로는 외부 호출마다 새 graph가 생성되는 환경을 보장할 수 없습니다. GAIA Agent 설정은 아래처럼 `GaiA Input`의 tweak만 채웁니다. 캔버스의 전용 edge가 metadata 세션 ID를 Router 00으로 전달합니다.

| 외부 요청 필드 | Flow 06 입력 |
| --- | --- |
| `input_value` | `GaiA Input.input_value` |
| `tweaks["GaiA Input"].data` | `GaiA Input.data` |
| `tweaks["GaiA Input"].metadata` | `GaiA Input.metadata` |

- `data`에는 선택적으로 `{"conversation_history":[...]}`를 보냅니다. GaiA Input은 list 값을 안전한 JSON 문자열로 Message에 보존하고, Router Mongo 상태가 없을 때에도 Context Loader는 여기서 직전 완료 질문·답변 한 쌍만 보조 문맥으로 복원합니다.
- `metadata.session_id`는 `GaiA 외부 세션 ID 추출` 컴포넌트가 세션 전용 `Message(text=session_id)`로 변환한 뒤 `00 Router 세션 문맥 로더.session_id`에 연결됩니다. **GaiA Input.message를 이 포트에 직접 연결하면 질문 본문이 세션 ID로 오인되므로 연결하지 않습니다.**
- 실행 trace에서 CUBE 요청, GAIA 요청, Context Loader의 canonical 세션 ID, 하위 Flow 세션 ID가 같은지 한 번 확인합니다.

분류만 필요한 운영 기본 경로에서는 API Smart Router가 더 빠를 수 있습니다. Agent Tool Router는 복합적인 자연어 분류 완성도와 관리 편의성을 비교 검증하기 위한 대안입니다.
