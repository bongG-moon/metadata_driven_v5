# Route Flow v2 연결 가이드 (07 Agent + Tool)

이 Flow는 기존 API Smart Router를 대체하지 않는 별도 대안입니다. Agent가 다섯 개 하위 Flow 도구 중 하나를 선택하고, 하위 Flow의 최종 답변을 단일 Chat Output으로 반환합니다.

## 구성

```text
Chat Input.message -> Agent.input_value
Cached Flow Tool 5종.component_as_tool -> Agent.tools
Agent.response -> Chat Output.input_value
```

- Chat Output은 정확히 하나입니다.
- Chat Input은 Agent에만 한 번 연결합니다. Tool 5개는 부모 `graph.session_id`를 자동 상속하므로 별도 세션 Message edge가 없습니다.
- 각 Tool의 `cache_flow`와 `return_direct`는 `true`입니다.
- `flow_id_selected`는 export에서 비워 둡니다. 최초 실행은 `flow_name_selected`로 실제 ID와 `updated_at`을 조회하고, 같은 component instance의 이후 실행은 해석된 실제 ID를 우선 재사용합니다.
- 이름/ID 조회와 그래프 캐시는 Langflow가 component에 주입한 현재 실행 `user_id`를 사용합니다. `_user_id`가 없을 때는 Langflow 기본 속성이 부모 `graph.user_id`를 사용하며, custom component가 이 읽기 전용 값을 덮어쓰지 않습니다. Router와 하위 Flow 5종은 반드시 같은 사용자로 import하고 같은 사용자 또는 API key로 실행해야 합니다.
- 그래프 캐시는 실제 `user_id + flow_id` 키를 사용하며, 대상 Flow가 갱신되면 `updated_at` 비교로 무효화됩니다. 저장된 ID가 유효하지 않거나 다른 이름을 가리키면 정확한 Flow 이름으로 다시 해석합니다.
- 캐시는 Flow 그래프 구성 비용만 줄입니다. 데이터 조회, pandas 실행, LLM 답변 결과는 매 요청 다시 실행합니다.

## Tool 매핑

| Tool | 대상 Flow |
| --- | --- |
| `run_data_analysis` | `metadata_driven_v5_complete_20260710_data_analysis` |
| `run_metadata_qa` | `metadata_driven_v5_complete_20260710_metadata_qa` |
| `save_domain_metadata` | `metadata_driven_v5_complete_20260710_domain_saving` |
| `save_table_catalog_metadata` | `metadata_driven_v5_complete_20260710_table_catalog_saving` |
| `save_main_flow_filter_metadata` | `metadata_driven_v5_complete_20260710_main_flow_filter_saving` |

## Tool schema 절감과 안정화

표준 Run Flow Tool은 Data Analysis Flow의 편집 가능한 Text Input 프롬프트까지 Agent 인자로 노출할 수 있습니다. 현재 export를 LFX 0.3.4 schema builder로 측정하면 표준 5개 필드는 26,338 bytes이고, 이 구현의 필수 `question` 한 필드는 339 bytes입니다. 내부 지시문, helper 코드, repair prompt는 기존 canvas에서 계속 편집할 수 있지만 Router Agent 토큰에는 포함되지 않습니다.

외부 Tool 인자는 항상 `flow_tweak_data.question`입니다. 실행 직전에 현재 그래프의 단일 Chat Input ID를 찾아 내부 `ChatInput-...~input_value` tweak로 변환합니다. 따라서 standalone import가 node ID를 다시 발급하거나 모델/provider가 Tool 필드명의 특수문자를 정규화하더라도 질문 필드가 바뀌지 않습니다.

## 성능 특성

- 첫 실행: Agent의 Tool 선택 + 하위 Flow 그래프 로드 + 하위 Flow 실행
- 이후 실행: Agent의 Tool 선택 + 캐시된 그래프 복원 + 하위 Flow 실행
- `return_direct=true`: Tool 실행 뒤 결과를 다시 쓰는 추가 Agent LLM 단계를 생략

분류만 필요한 운영 기본 경로에서는 API Smart Router가 더 빠를 수 있습니다. Agent Tool Router는 복합적인 자연어 분류 완성도와 관리 편의성을 비교 검증하기 위한 대안입니다.
