# Route Flow v2 연결 가이드 (07 Agent + Tool)

이 Flow는 기존 API Smart Router를 대체하지 않는 별도 대안입니다. Agent가 다섯 개 하위 Flow 도구 중 하나를 선택하고, 하위 Flow의 최종 답변을 단일 Chat Output으로 반환합니다.

## 구성

```text
Chat Input.message -> Agent.input_value
Cached Flow Tool 5종.component_as_tool -> Agent.tools
Agent.response -> Chat Output.input_value
```

- Chat Output은 정확히 하나입니다.
- Chat Input은 Agent에만 한 번 연결합니다. Tool 5개는 LFX Tool wrapper가 `_pre_run_setup()`을 건너뛰는 경로에서도 실행 직전에 부모 runtime/graph `session_id`를 확정하므로 별도 세션 Message edge가 없습니다.
- 각 Tool의 `cache_flow`와 `return_direct`는 `true`입니다.
- Router Agent의 `n_messages`는 `5`, `max_iterations`는 `1`입니다. 이 값은 현재 저장 메시지 1개와 이전 사용자/응답 2턴 4개를 조회합니다. GaiA Input Adapter가 원본 Message ID를 보존하고 LFX Agent가 `input_value`와 ID가 같은 현재 메시지를 history에서 제거하므로, 모델에는 현재 질문이 중복되지 않은 이전 2턴만 전달됩니다. 선택된 Data Analysis Flow는 같은 부모 세션으로 저장된 분석 상태도 함께 복원합니다.
- `flow_name_selected`는 기본 Run Flow처럼 새로고침 가능한 Flow 선택 드롭다운입니다. 실제 환경에서는 하위 Flow를 import한 뒤 각 Tool 노드에서 목록을 새로고침하고 대상을 다시 선택하면 현재 Flow ID가 `flow_id_selected`에 저장됩니다.
- 기본 `Flow 해석 방식=Flow ID 우선`은 선택된 실제 ID가 있으면 이름 검색 없이 ID로 실행하고, standalone import 직후 ID가 비어 있을 때만 export된 이름으로 조회합니다.
- 이름 검색을 완전히 금지하려면 각 Tool 노드에서 `Flow 해석 방식=선택한 Flow ID만`을 선택합니다. 이 모드에서 ID가 비어 있으면 잘못된 Flow를 실행하지 않고 대상 Flow 재선택 안내 오류를 반환합니다.
- `flow_id_selected`는 환경마다 달라지므로 repository export에서는 계속 비워 둡니다. 실제 환경에서 드롭다운을 다시 선택해 현재 ID를 저장해야 합니다.
- 이름/ID 조회와 그래프 캐시는 Langflow가 component에 주입한 현재 실행 `user_id`를 사용합니다. `_user_id`가 없을 때는 Langflow 기본 속성이 부모 `graph.user_id`를 사용하며, custom component가 이 읽기 전용 값을 덮어쓰지 않습니다. Router와 하위 Flow 5종은 반드시 같은 사용자로 import하고 같은 사용자 또는 API key로 실행해야 합니다.
- 그래프 캐시는 실제 `user_id + flow_id` 키를 사용하며, 대상 Flow가 갱신되면 `updated_at` 비교로 무효화됩니다.
- 캐시는 Flow 그래프 구성 비용만 줄입니다. 데이터 조회, pandas 실행, LLM 답변 결과는 매 요청 다시 실행합니다.
- Langflow 1.9.2/LFX 0.4.2에서 `return_direct` Tool 결과가 Agent의 단계 카드에만 남고 본문이 비는 경우를 GaiA Output Adapter가 마지막 완료 Tool의 `content`에서 복원합니다. 따라서 하위 Flow 답변을 다시 LLM으로 재작성하지 않습니다.
- 하위 Flow에 화면용 `Chat Output.message`와 API용 구조화 terminal이 함께 있어도 Route V2는 현재 Chat Output만 실행 출력으로 활성화합니다. 다른 terminal은 해당 child 실행에서만 비활성화하므로 `return_direct=true` 결과가 하나의 Message로 끝납니다.

## Tool 매핑

| Tool | 대상 Flow |
| --- | --- |
| `run_data_analysis` | `01. v5_data_analysis` |
| `run_metadata_qa` | `05. v5_metadata_qa` |
| `save_domain_metadata` | `02. v5_domain_saving` |
| `save_table_catalog_metadata` | `03. v5_table_catalog_saving` |
| `save_main_flow_filter_metadata` | `04. v5_main_flow_filter_saving` |

## Tool schema 절감과 안정화

표준 Run Flow Tool은 Data Analysis Flow의 편집 가능한 Text Input 프롬프트까지 Agent 인자로 노출할 수 있습니다. 현재 export 기준 표준 5개 필드는 약 26KB이고, 이 구현은 필수 `question` 한 필드만 공개합니다. 내부 지시문, helper 코드, repair prompt는 기존 canvas에서 계속 편집할 수 있지만 Router Agent 토큰에는 포함되지 않습니다.

외부 Tool 인자는 항상 `flow_tweak_data.question`입니다. 실행 직전에 현재 그래프의 단일 Chat Input ID를 찾아 내부 `ChatInput-...~input_value` tweak로 변환합니다. 따라서 standalone import가 node ID를 다시 발급하거나 모델/provider가 Tool 필드명의 특수문자를 정규화하더라도 질문 필드가 바뀌지 않습니다.

## 성능 특성

- 첫 실행: Agent의 Tool 선택 + 하위 Flow 그래프 로드 + 하위 Flow 실행
- 이후 실행: Agent의 Tool 선택 + 캐시된 그래프 복원 + 하위 Flow 실행
- `return_direct=true`: Tool 실행 뒤 결과를 다시 쓰는 추가 Agent LLM 단계를 생략

분류만 필요한 운영 기본 경로에서는 API Smart Router가 더 빠를 수 있습니다. Agent Tool Router는 복합적인 자연어 분류 완성도와 관리 편의성을 비교 검증하기 위한 대안입니다.
