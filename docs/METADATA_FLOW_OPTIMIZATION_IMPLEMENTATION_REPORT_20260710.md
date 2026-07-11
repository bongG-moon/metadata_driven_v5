# Metadata Saving · QA · API Router 최적화 구현 보고서

- 구현일: 2026-07-10
- 대상: `metadata_driven_v5`
- MongoDB 정책: 기존 컬렉션과 문서 schema 유지
- 제외 사항: 사용자 결정에 따라 `revision`, `request_id`, idempotency 필드 및 별도 이력 컬렉션은 추가하지 않음

> 2026-07-11 갱신: 현재 import bundle은 하위 Flow 5개, API Router, Agent + Tool Mode Router의 7개 Flow입니다. API Router의 중복 질문 JSON terminal Gate는 제거됐고, 아래 수치와 적용 절차는 현재 bundle 기준으로 갱신했습니다.

## 1. 구현 결과

외부 감사에서 확인한 저장 안전성, payload 중복, QA context 과다 전달, Router 관측성 문제를 수정했다. 이후 Data Analysis도 현재 단일 executor repair와 metadata 후보 최적화 구조로 갱신되어 있으며 최신 전체 상태는 `V5_IMPLEMENTATION_REPORT.md`를 함께 기준으로 한다.

### Metadata Saving 3종

- 별도 refinement Agent를 standalone Flow에서 제거했다.
- 기존 metadata 500건 전체 로더를 standalone Flow에서 제거했다.
- `원문 -> metadata 추출 Agent -> 후보 key MongoDB 조회` 순서로 변경했다.
- Dry Run과 실저장 모두 추출 LLM 1회 뒤 결정론 Writer 검수를 사용한다. 별도 review LLM은 호출하지 않는다.
- standalone에서 대기·재개할 수 없는 `ask`는 제거했으며 입력되면 `skip`으로 정규화한다. `skip`, `merge`, `replace`, `create_new`는 서로 다른 동작이다.
- `create_new`는 기존 key 뒤에 `_copy`, `_copy_2`를 붙여 같은 schema의 새 문서를 만든다.
- Table Catalog `source_config` allowlist와 재귀 secret-key 차단을 추가했다.
- Domain/Main Filter에는 source/query config 금지 검사를 유지·강화했다.
- `registration_trace.raw_text` 필드 위치는 유지하면서 secret redaction과 2,000자 제한을 적용했다.
- `dry_run` 입력을 Bool toggle로 변경했다.
- 부분 write 오류가 발생하면 완료된 operation과 `partial_success` 상태를 반환한다.

### Metadata QA

- MongoDB loader가 identity, `status`, `payload`만 projection하도록 변경했다.
- 질문 유형을 먼저 판별한 뒤 필요한 필드만 LLM context에 포함한다.
- `available_sources` 질문에는 SQL/query template을 전달하지 않는다.
- `dataset_sql` 질문에만 선택된 dataset의 SQL을 전달한다.
- `max_items`가 실제 상한으로 동작하도록 수정했다.
- `max_bytes`를 추가했으며 기본값은 65,536 bytes다.
- `POP` 도메인 설명과 제품 조건 분기 충돌을 정리했다.
- 표 rows는 `data.rows`에만 두고 `answer_sections.detail_table.row_source=data.rows`로 참조한다.
- `response_type=metadata_qa`, `direct_response_ready=true`, 결정론 fallback은 유지했다.

### 06 API Router

- Smart Router + 하위 Langflow Run API 방식은 그대로 유지했다.
- API key 입력을 `SecretStrInput`으로 변경했다.
- `requests.Session`을 재사용해 HTTP connection pooling을 적용했다.
- connect timeout 5초, read timeout 240초를 분리했다. 외부 Web/API client 기본 timeout은 300초로 맞췄다.
- 저장 route를 포함해 자동 retry는 추가하지 않았다.
- Chat Input은 Smart Router에만 연결한다. Langflow가 API caller의 `session_id` 입력에 부모 세션을 자동 주입하므로 별도 `session_source` fan-out은 사용하지 않는다.
- 표시용 `message`와 운영용 `status_data` output을 분리했다.
- 두 output을 모두 읽어도 컴포넌트 cache를 사용해 하위 API는 한 번만 호출한다.
- `status_data`에는 route, HTTP/downstream status, duration, session, errors/warnings만 포함한다.

## 2. MongoDB 호환성 및 최종 운영 결정

초기 구현의 별도 v5 collection 사용 방침은 최종 운영 결정에서 철회했다. v5는 아래 v4 collection을 직접 공유한다.

- `agent_v4_domain_items`
- `agent_v4_table_catalog_items`
- `agent_v4_main_flow_filters`
- Domain `_id=domain:{section}:{key}`
- Table Catalog `_id=table_catalog:{dataset_key}`
- Main Filter `_id=main_flow_filter:{filter_key}`
- `status`, `payload`, `source_config` 위치와 기존 필드 의미
- `registration_trace.raw_text` 위치

따라서 v4 데이터를 v5용으로 복사하거나 migration할 필요가 없다. `merge`, `replace`, `create_new`가 만드는 문서도 기존 v4 collection에 같은 schema로 저장된다.

## 3. Standalone Flow exports

| Flow | 파일 | 노드 | Edge |
|---|---|---:|---:|
| Domain Saving | `flow_exports/domain_saving_flow_v5_standalone.json` | 13 | 14 |
| Table Catalog Saving | `flow_exports/table_catalog_saving_flow_v5_standalone.json` | 13 | 14 |
| Main Flow Filter Saving | `flow_exports/main_flow_filter_saving_flow_v5_standalone.json` | 13 | 14 |
| Metadata QA | `flow_exports/metadata_qa_flow_v5_standalone.json` | 13 | 16 |
| API Router | `flow_exports/api_router_flow_v5_standalone.json` | 14 | 13 |
| Agent Tool Router | `flow_exports/agent_tool_router_flow_v5_standalone.json` | 8 | 7 |

재생성 명령:

```powershell
$py='C:\Users\qkekt\AppData\Local\com.LangflowDesktop\.langflow-venv\Scripts\python.exe'
& $py tools\build_v5_auxiliary_flows.py
```

API Router는 고정 endpoint를 사용하고 Agent Tool Router는 이름으로 실제 Flow ID를 해석하므로 import 뒤 ID를 교체할 필요가 없다. Smart Router/Agent model provider credential은 운영 Langflow 설정 또는 Global Variable에 연결해야 한다.

## 4. 검증 결과

### Python 계약 테스트

- 전체 test suite: 222 passed
- 포함 범위: Data Analysis 기존 회귀, metadata saving, QA, Router, web response normalization, standalone export 구조

### 실제 Langflow/LFX

- Langflow 1.8.2
- LFX 0.3.4
- 변경 핵심 행동 테스트: actual LFX 환경 6 passed
- 대상 Custom Component source: 49개 parser 통과
- standalone export node template: 총 115/115 통과

### 실제 임시 서버 import

격리된 `LANGFLOW_CONFIG_DIR`, 별도 SQLite, `127.0.0.1:7867` 임시 서버에서 7개 Flow 모두 HTTP 201로 import했다.

- Domain Saving: 13 nodes / 14 edges
- Table Catalog Saving: 13 nodes / 14 edges
- Main Flow Filter Saving: 13 nodes / 14 edges
- Metadata QA: 13 nodes / 16 edges
- API Router: 14 nodes / 13 edges
- Agent Tool Router: 8 nodes / 7 edges

LLM/API key가 필요한 노드 직전까지 partial build도 모두 통과했다.

- 저장 3종: `Chat Input -> Request -> Variables`
- Metadata QA: `Chat Input + Mongo loaders -> Context Builder`
- Router: Chat Input vertex

검증 후 임시 서버는 종료했다. 운영 MongoDB와 기존 Langflow 데이터는 변경하지 않았다.

## 5. 운영 적용 전 남은 설정

코드/Flow 구현은 완료됐지만 실제 회사 환경 연결에는 다음 값이 필요하다.

1. 단일 `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json` 또는 `import_ready_flows`의 7개 JSON을 파일명 순서대로 import한다.
2. Router는 고정 endpoint 경로가 입력되어 있으므로 Flow ID 치환이나 edge 재연결이 필요 없다.
3. 기본 주소가 `http://127.0.0.1:7860`이 아니면 `LANGFLOW_BASE_URL`을 설정한다. 인증 환경에서는 `LANGFLOW_API_KEY`도 설정한다.
4. Smart Router와 Agent의 model/provider credential을 운영 Global Variable 또는 provider 설정에 연결한다.
5. `MONGODB_URI`, `MONGODB_DATABASE`, 세 metadata collection 환경변수를 설정한다.
6. 저장 Flow는 먼저 `dry_run=true`로 확인한 뒤 실저장이 필요한 Flow만 toggle을 끈다.
7. 실제 metadata 예시 질문, live MongoDB duplicate action, Router 2-turn session을 운영 환경에서 최종 smoke test한다.

실제 LLM과 운영 MongoDB credential은 현재 환경에 없으므로 production E2E 저장은 수행하지 않았다. 대신 fake MongoDB 행동 검증, actual-LFX component 검증, 실제 Langflow import/partial build까지 완료했다.
