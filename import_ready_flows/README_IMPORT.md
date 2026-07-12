# Metadata Driven v5 완전 연결 Langflow JSON

이 폴더의 JSON은 Langflow 1.8.2 standalone 환경에 바로 import할 수 있도록 모든 canvas edge와 Router 하위 endpoint를 미리 연결한 묶음입니다.

## 가장 간단한 Import 방법

Langflow의 Flow 화면에서 아래 파일 **하나만** 선택합니다.

`00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`

Langflow UI가 최상위 `flows` 배열을 펼쳐 7개 Flow를 한 번에 import합니다. 이 파일은 UTF-8 BOM 없이 minified JSON으로 생성되며 첫 바이트가 정확히 `{"flows":[`입니다.

## 개별 Import 방법

파일명 앞 번호 순서대로 `01`부터 `07`까지 import합니다. `06`은 운영 기본 API Router이고, `07`은 비교용 Agent + Tool Mode Router입니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 1 | `01_data_analysis_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis` | 42 | 68 |
| 2 | `02_domain_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-domain-saving` | 13 | 14 |
| 3 | `03_table_catalog_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-table-catalog-saving` | 13 | 14 |
| 4 | `04_main_flow_filter_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-main-flow-filter-saving` | 13 | 14 |
| 5 | `05_metadata_qa_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-metadata-qa` | 11 | 18 |
| 6 | `06_api_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-api-router` | 14 | 13 |
| 7 | `07_agent_tool_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router` | 8 | 7 |

## 수동 연결 여부

- canvas edge 재연결: 필요 없음
- Router Flow ID 치환: 필요 없음
- Router URL 5개 개별 입력: 필요 없음
- Agent Tool Router Flow ID 재연결: 필요 없음

Router는 고정 `endpoint_name` 경로를 사용합니다. 같은 bundle을 다시 import하면 Langflow가 endpoint에 `-1`을 붙일 수 있으므로, 재import 시에는 기존 `metadata-driven-v5-complete-20260710-*` Flow를 먼저 정리합니다.

## 환경 설정

- 기본 Langflow 주소: `http://127.0.0.1:7860`
- 다른 주소/포트: `LANGFLOW_BASE_URL` 설정
- 인증 사용: `LANGFLOW_API_KEY` 설정
- Router 하위 Flow read timeout: 240초
- 외부 Web/API client timeout 권장값: 300초 (`LANGFLOW_TIMEOUT_SECONDS=300`)
- Gemini/provider credential: Langflow Model Providers 또는 Global Variable 설정
- MongoDB: Langflow Credential Global Variable `MONGO_URL` 생성 후 import된 Mongo 노드의 바인딩 확인
- MongoDB database: `datagov`
- v4 공유 collection: `agent_v4_domain_items`, `agent_v4_table_catalog_items`, `agent_v4_main_flow_filters`, `agent_v4_result_store`, `agent_v4_session_states`
- v4 데이터를 v5로 복사하지 않고 기존 collection을 직접 사용
- 실제 Mongo URI는 JSON에 포함되지 않으며 Python 컴포넌트는 OS `MONGODB_URI` fallback을 사용하지 않음
- Data Analysis dummy/live 단일 설정: `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode`
- `07 데이터 조회 작업 라우터`에는 별도 모드 설정이 없으며 `04A`가 payload에 기록한 값을 사용
- 저장 Flow: 안전을 위해 `dry_run=true`가 기본값이며 실제 저장 시에만 의도적으로 끕니다.

## 검증 결과

- 전체 pytest: 254 passed
- 커스텀 원본 동기화: export/개별 import/통합 bundle 각각 77/77 노드가 실제 Python 원본 68개에 매핑, 누락 0
- 한글 설명/인코딩: Python 69/69와 함수 1086/1086, JSON 내장 함수 3600/3600 및 ZIP 10개 entry에서 strict UTF-8·BOM 없음·깨짐 문자 없음·JSON parse 확인
- 대표 Dummy 질문: 23/23 통과
- Langflow 1.8.2 frontend edge handle codec: 296/296 parse 및 `edge.data` 일치
- Langflow 1.8.2 연결 규칙: advanced component input을 대상으로 하는 edge 0건
- Langflow 1.8.2 / LFX 0.3.4 node template: 114/114 passed
- API Router 직접 응답/명확화 분기: 예전 정상 Flow와 같은 Smart Router -> Chat Output 직접 edge 2/2, FinalGate 0개
- API Router 단일 진입 구조: Chat Input -> Smart Router edge 1개, API caller용 session fan-out edge 0개
- Router 세션: Langflow가 각 API caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 별도 Message edge 없이 유지
- 격리 Langflow 서버 import: 7/7 HTTP 201
- 하위 Flow 5개와 Agent Tool Router: Chat Output 1개씩 확인
- Data Analysis: executor node 1개, 초기 성공 시 Repair LLM 0회, 실행 오류 시 이전 코드·오류 문맥을 전달해 최대 1회 복구, 단일 최종화 체인 확인
- Data Analysis Repair Prompt: `17B pandas 복구 프롬프트 템플릿` visible Text Input에서 원문을 관리하고 executor의 non-advanced 입력에 연결
- pandas import 정책: 정확한 `import pandas as pd`, `import numpy as np`만 실제 import 없이 정규화하고, 기타 import와 파일·네트워크 I/O는 차단
- pandas safe builtin 정책: `zip`을 executor namespace에서 제공해 `dict(zip(...))`가 불필요한 Repair LLM을 유발하지 않음
- API Router는 Run Flow 노드가 0개입니다. Agent Tool Router는 이름 기반 Cached Run Flow Tool 5개 모두 `cache_flow=true`, `return_direct=true`, 고정 Flow ID 없음으로 구성됩니다.
- Agent Tool Router의 Tool schema에는 node ID가 없는 필수 `question` 하나만 포함합니다. 실행 직전에 현재 그래프의 단일 Chat Input ID로 내부 변환하며, Data Analysis 기준 표준 26,338 bytes에서 339 bytes로 줄었습니다. 내부 Prompt/Helper/Repair Text Input은 제외됩니다.
- Agent Tool Router는 `session_source` 포트와 edge 없이 부모 `graph.session_id`를 자동 상속합니다. Chat Input은 Agent에만 한 번 연결됩니다.
- 격리 import에서 새로 발급된 Data Analysis Flow ID를 이름으로 해석하고 `CachedFlowTool-data_analysis`까지 실제 partial build를 통과했습니다.
- Metadata 저장 Flow 3종: Existing Loader를 Matcher에 직접 연결하고 단일 Writer/Response/Chat Output 사용
- Metadata 저장·조회 MongoDB 설정: 일반 노드 14개와 QA 통합 snapshot 노드 1개(컬렉션 3종)에 database/collection 기본값 명시
- Metadata 후보: 도메인 관련 항목 최대 10건, 테이블 최소 5/최대 10건, 메인 필터 전체, compact JSON 32KB 정책과 장비+UPH 질문 회귀 검증
