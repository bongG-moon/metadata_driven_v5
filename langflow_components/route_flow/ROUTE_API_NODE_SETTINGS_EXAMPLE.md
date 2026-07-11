# Route Flow Route별 API URL 설정 예시 (06)

각 route branch마다 `01 선택 Flow API 메시지 호출기`를 하나씩 둡니다.
완전 연결 bundle에서는 각 노드에 아래 `endpoint_name` 경로가 이미 입력됩니다.
현재 API caller에는 별도 `session_source` 입력이 없습니다. Langflow가 부모 실행 session_id를 각 caller의 `session_id` 입력에 자동 주입합니다. Chat Input은 Smart Router에만 연결해야 합니다.
API key는 Secret 입력 또는 Langflow Global Variable을 사용하고 Flow JSON에 평문을 저장하지 않습니다.

| Route branch | `01 하위 Flow API URL` |
| --- | --- |
| `data_analysis` | `/api/v1/run/metadata-driven-v5-complete-20260710-data-analysis` |
| `metadata_qa` | `/api/v1/run/metadata-driven-v5-complete-20260710-metadata-qa` |
| `domain_saving` | `/api/v1/run/metadata-driven-v5-complete-20260710-domain-saving` |
| `table_catalog_saving` | `/api/v1/run/metadata-driven-v5-complete-20260710-table-catalog-saving` |
| `main_flow_filter_saving` | `/api/v1/run/metadata-driven-v5-complete-20260710-main-flow-filter-saving` |

상대 경로는 `LANGFLOW_BASE_URL` 또는 `LANGFLOW_API_BASE_URL`을 사용하며 둘 다 없으면 `http://127.0.0.1:7860`을 사용합니다. 인증 환경에서는 `LANGFLOW_API_KEY`를 설정합니다.

## 공통 연결

```text
Chat Input.message
  -> Smart Router.input

Smart Router.<route output>
  -> 01 선택 Flow API 메시지 호출기.flow_input

Chat Input.message
  -> Smart Router.input

01 선택 Flow API 메시지 호출기.message
  -> Chat Output.input
```

## Smart Router Route Message 설정

API 호출 route의 Route Message는 비워둡니다.
`direct_answer`, `clarification`처럼 하위 flow를 호출하지 않는 route에만 사용자에게 보여줄 메시지를 입력합니다.

API 호출 route에서 Smart Router output에 `오늘 WB공정에서 제품별 생산량을 알려줘`처럼 사용자 질문 원문이 보이면 정상입니다.
그 원문이 `01.flow_input`으로 들어가고, `01`은 Langflow Run API에 `input_value`와 `session_id`를 함께 전달합니다.
