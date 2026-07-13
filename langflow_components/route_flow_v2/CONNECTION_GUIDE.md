# Route Flow v2 연결 가이드 (07 Agent + Tool)

이 Flow는 기존 API Smart Router를 대체하지 않는 별도 대안입니다. Agent가 다섯 개 하위 Flow 도구 또는 명시적 요청 전용 실행 환경 진단 도구 중 하나를 선택하고, 결과를 단일 Chat Output으로 반환합니다.

## 구성

```text
Chat Input.message -> Agent.input_value
Cached Flow Tool 5종.component_as_tool -> Agent.tools
Route V2 실행 환경 진단 도구.component_as_tool -> Agent.tools
Agent.response -> Chat Output.input_value
```

- Chat Output은 정확히 하나입니다.
- Chat Input은 Agent에만 한 번 연결합니다. 하위 Flow Tool 5개는 부모 `graph.session_id`를 자동 상속하므로 별도 세션 Message edge가 없습니다.
- 각 Tool의 `cache_flow`와 `return_direct`는 `true`입니다.
- `flow_id_selected`는 비워 둡니다. Import 환경마다 DB ID가 달라지므로 실행 시 `flow_name_selected`로 실제 ID와 `updated_at`을 조회합니다.
- 그래프 캐시는 실제 `user_id + flow_id` 키를 사용하며, 대상 Flow가 갱신되면 `updated_at` 비교로 무효화됩니다.
- 캐시는 Flow 그래프 구성 비용만 줄입니다. 데이터 조회, pandas 실행, LLM 답변 결과는 매 요청 다시 실행합니다.

## Tool 매핑

| Tool | 대상 Flow |
| --- | --- |
| `run_data_analysis` | `metadata_driven_v5_complete_20260710_data_analysis` |
| `run_metadata_qa` | `metadata_driven_v5_complete_20260710_metadata_qa` |
| `save_domain_metadata` | `metadata_driven_v5_complete_20260710_domain_saving` |
| `save_table_catalog_metadata` | `metadata_driven_v5_complete_20260710_table_catalog_saving` |
| `save_main_flow_filter_metadata` | `metadata_driven_v5_complete_20260710_main_flow_filter_saving` |
| `diagnose_route_v2_environment` | 하위 Flow를 실행하지 않고 현재 Route 요청의 사용자·Flow 이름·버전·그래프 계약을 점검 |

## 실행 환경 진단 사용법

07 Router를 실제로 호출하는 동일한 경로에서 다음처럼 질문합니다.

```text
Route V2의 Run Flow 오류 원인을 진단해줘
```

진단 결과에는 현재 요청의 실행 사용자 해시 참조, 정확한 하위 Flow 이름 가시성, 중복 import suffix, Langflow/LFX 버전과 Run Flow 내부 method 계약, 하위 Flow의 Chat Input/Output/terminal 개수가 표시됩니다. 하위 Flow는 실제 실행하지 않으며 원본 사용자 ID·Flow ID·API Key·환경변수와 예외 원문은 표시하지 않습니다.

판정 코드의 의미는 다음과 같습니다.

| 판정 코드 | 의미 |
| --- | --- |
| `RUNTIME_USER_MISSING` | Route V2 실행 문맥에 사용자 ID가 전달되지 않음 |
| `RUNTIME_USER_INVALID` | 전달된 사용자 ID가 Langflow 조회에 필요한 UUID 형식이 아님 |
| `CURRENT_USER_FLOW_LIST_FAILED` | 현재 실행 사용자 범위의 Flow 목록 조회 자체가 실패함 |
| `TARGET_NOT_VISIBLE_IN_RUNTIME_SCOPE` | 현재 요청 사용자에게 정확한 이름의 하위 Flow가 보이지 않음. 이름 또는 소유권 차이 가능 |
| `TARGET_NAME_SUFFIX_MISMATCH` | `(1)` 같은 중복 import 이름만 보임 |
| `USER_CONTEXT_MISMATCH` | 컴포넌트와 부모 graph의 사용자 문맥이 다름 |
| `RUN_FLOW_CONTRACT_MISMATCH` | 현재 LFX의 Run Flow 내부 method/signature가 구현 기준과 다름 |
| `RUNTIME_VERSION_MISMATCH` | Langflow/LFX 버전이 검증 기준 1.8.2/0.3.4와 다름 |
| `CHILD_FLOW_TOPOLOGY_MISMATCH` | 하위 Flow의 Chat Input/Output 또는 terminal output 개수가 다름 |
| `PREFLIGHT_OK_CHILD_EXECUTION_FAILED` | 사전 점검 통과. 질문 tweak 또는 하위 Flow 내부 실행 로그 확인 필요 |

## Tool schema 절감과 안정화

표준 Run Flow Tool은 Data Analysis Flow의 편집 가능한 Text Input 프롬프트까지 Agent 인자로 노출할 수 있습니다. 현재 export를 LFX 0.3.4 schema builder로 측정하면 표준 5개 필드는 26,338 bytes이고, 이 구현의 필수 `question` 한 필드는 339 bytes입니다. 내부 지시문, helper 코드, repair prompt는 기존 canvas에서 계속 편집할 수 있지만 Router Agent 토큰에는 포함되지 않습니다.

외부 Tool 인자는 항상 `flow_tweak_data.question`입니다. 실행 직전에 현재 그래프의 단일 Chat Input ID를 찾아 내부 `ChatInput-...~input_value` tweak로 변환합니다. 따라서 standalone import가 node ID를 다시 발급하거나 모델/provider가 Tool 필드명의 특수문자를 정규화하더라도 질문 필드가 바뀌지 않습니다.

## 성능 특성

- 첫 실행: Agent의 Tool 선택 + 하위 Flow 그래프 로드 + 하위 Flow 실행
- 이후 실행: Agent의 Tool 선택 + 캐시된 그래프 복원 + 하위 Flow 실행
- `return_direct=true`: Tool 실행 뒤 결과를 다시 쓰는 추가 Agent LLM 단계를 생략

분류만 필요한 운영 기본 경로에서는 API Smart Router가 더 빠를 수 있습니다. Agent Tool Router는 복합적인 자연어 분류 완성도와 관리 편의성을 비교 검증하기 위한 대안입니다.
