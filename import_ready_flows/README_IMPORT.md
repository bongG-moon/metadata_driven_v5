# Metadata Driven v5 완전 연결 Langflow JSON

이 폴더의 JSON은 Langflow 1.8.2 standalone 환경에 바로 import할 수 있도록 모든 canvas edge와 Router 하위 endpoint를 미리 연결한 묶음입니다.

## 가장 간단한 Import 방법

Langflow의 Flow 화면에서 아래 파일 **하나만** 선택합니다.

`00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`

Langflow UI가 최상위 `flows` 배열을 펼쳐 10개 Flow를 한 번에 import합니다. 이 파일은 UTF-8 BOM 없이 minified JSON으로 생성되며 첫 바이트가 정확히 `{"flows":[`입니다.

## 개별 Import 방법

파일명 앞 번호 순서대로 `01`부터 `10`까지 import합니다. `06`은 운영 기본 API Router, `07`은 단일 호출용 Agent + Tool Mode Router, `08`은 등록 또는 자연어 Workflow를 기본 Loop로 실행하는 Workflow Orchestrator, `09`는 Workflow Skill 등록·검토·저장 Flow, `10`은 Data Analysis 결과 참조를 HTML 차트로 만드는 Flow입니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 1 | `01_data_analysis_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis` | 43 | 67 |
| 2 | `02_domain_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-domain-saving` | 13 | 14 |
| 3 | `03_table_catalog_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-table-catalog-saving` | 13 | 14 |
| 4 | `04_main_flow_filter_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-main-flow-filter-saving` | 13 | 14 |
| 5 | `05_metadata_qa_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-metadata-qa` | 11 | 17 |
| 6 | `06_api_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-api-router` | 14 | 13 |
| 7 | `07_agent_tool_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router` | 8 | 7 |
| 8 | `08_workflow_orchestrator_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-workflow-orchestrator` | 18 | 26 |
| 9 | `09_workflow_skill_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-workflow-skill-saving` | 13 | 14 |
| 10 | `10_html_visualization_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-html-visualization` | 4 | 3 |

## 수동 연결 여부

- canvas edge 재연결: 필요 없음
- Router Flow ID 치환: 필요 없음
- Router URL 5개 개별 입력: 필요 없음
- Agent Tool Router Flow ID 재연결: 필요 없음
- Workflow Orchestrator Flow ID 재연결: 필요 없음

Router는 고정 `endpoint_name` 경로를 사용합니다. 같은 bundle을 다시 import하면 Langflow가 endpoint에 `-1`을 붙일 수 있으므로, 재import 시에는 기존 `metadata-driven-v5-complete-20260710-*` Flow를 먼저 정리합니다.

## 환경 설정

- 기본 Langflow 주소: `http://127.0.0.1:7860`
- 다른 주소/포트: `LANGFLOW_BASE_URL` 설정
- 인증 사용: `LANGFLOW_API_KEY` 설정
- Router 하위 Flow read timeout: 240초
- 외부 Web/API client timeout 권장값: 단일 호출 300초, Workflow 연계 호출 600초
- Gemini/provider credential: Langflow Model Providers 또는 Global Variable 설정
- MongoDB: Langflow Credential Global Variable `MONGO_URL` 생성 후 import된 Mongo 노드의 바인딩 확인
- MongoDB database: `datagov`
- v4 공유 collection: `agent_v4_domain_items`, `agent_v4_table_catalog_items`, `agent_v4_main_flow_filters`, `agent_v4_result_store`, `agent_v4_session_states`, `agent_v4_workflow_skills`
- v4 데이터를 v5로 복사하지 않고 기존 collection을 직접 사용
- 실제 Mongo URI는 JSON에 포함되지 않으며 Python 컴포넌트는 OS `MONGODB_URI` fallback을 사용하지 않음
- Data Analysis dummy/live 단일 설정: `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode`
- `07 데이터 조회 작업 라우터`에는 별도 모드 설정이 없으며 `04A`가 payload에 기록한 값을 사용
- 저장 Flow: 안전을 위해 `dry_run=true`가 기본값이며 실제 저장 시에만 의도적으로 끕니다.

## 검증 결과

- 전체 pytest: 335 passed
- 커스텀 원본 동기화: export/개별 import/통합 bundle 각각 97/97 노드가 실제 Python 원본 84개에 매핑, 누락 0
- 한글 설명/인코딩: Python·JSON·ZIP 전체에서 strict UTF-8·BOM 없음·깨짐 문자 없음·JSON parse 확인
- 대표 Dummy 질문: 31/31 통과
- Langflow 1.8.2 frontend edge handle codec: 378/378 parse 및 `edge.data` 일치
- Langflow 1.8.2 연결 규칙: advanced component input을 대상으로 하는 edge 0건
- Langflow 1.8.2 / LFX 0.3.4 node template: 150/150 passed
- Tool 없는 모델 단계와 Workflow 계획/최종 합성은 기본 Language Model을 사용하고, 단일 호출 Route V2만 실제 Tool이 연결된 기본 Agent를 유지
- API Router 직접 응답/명확화 분기: 예전 정상 Flow와 같은 Smart Router -> Chat Output 직접 edge 2/2, FinalGate 0개
- API Router 단일 진입 구조: Chat Input -> Smart Router edge 1개, API caller용 session fan-out edge 0개
- Router 세션: Langflow가 각 API caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 별도 Message edge 없이 유지
- 기존 8개 Flow의 격리 Langflow 서버 import는 검증 완료했으며, Workflow Orchestrator는 이번 bundle/node/edge 계약 검증 후 다음 live-server import 대상입니다.
- 통합 `00` 단일 JSON은 10개 Flow를 포함하도록 생성하고 UTF-8/BOM/flow count를 검증합니다.
- 하위 Flow 7개, Route V2, Workflow Orchestrator: Chat Output 1개씩 확인
- Data Analysis: executor node 1개, 초기 성공 시 Repair LLM 0회, 실행 오류 시 이전 코드·오류 문맥을 전달해 최대 1회 복구, 단일 최종화 체인 확인
- Data Analysis Repair Prompt: `17B pandas 복구 프롬프트 템플릿` visible Text Input에서 원문을 관리하고 executor의 non-advanced 입력에 연결
- pandas import 정책: 정확한 `import pandas as pd`, `import numpy as np`만 실제 import 없이 정규화하고, 기타 import와 파일·네트워크 I/O는 차단
- pandas safe builtin 정책: `zip`을 executor namespace에서 제공해 `dict(zip(...))`가 불필요한 Repair LLM을 유발하지 않음
- API Router는 Run Flow 노드가 0개입니다. Agent Tool Router는 이름 기반 Cached Run Flow Tool 5개 모두 Langflow의 현재 실행 `user_id` 범위에서 조회하며, `cache_flow=true`, `return_direct=true`, 고정 Flow ID 없음으로 구성됩니다. 최초 이름 조회 뒤에는 해석된 실제 ID를 우선 재사용합니다.
- Agent Tool Router의 Tool schema에는 node ID가 없는 필수 `question` 하나만 포함합니다. 실행 직전에 현재 그래프의 단일 Chat Input ID로 내부 변환하며, Data Analysis 기준 표준 26,338 bytes에서 339 bytes로 줄었습니다. 내부 Prompt/Helper/Repair Text Input은 제외됩니다.
- Agent Tool Router는 `session_source` 포트와 edge 없이 부모 `graph.session_id`를 자동 상속합니다. Chat Input은 Agent에만 한 번 연결됩니다.
- 격리 import에서 현재 Langflow 실행 사용자로 새로 발급된 Data Analysis Flow ID를 이름으로 해석하고 `CachedFlowTool-data_analysis`까지 실제 partial build를 통과했습니다.
- Workflow Orchestrator의 이름 기반 Tool 6개는 `question`과 선택 `upstream_result_ref`만 노출하고, 하위 API 응답을 `route_v3.tool_result.v1` compact observation으로 변환합니다.
- Workflow Orchestrator는 기본 Language Model 계획기 -> `workflow.plan.v1` 파서 -> 기본 Loop -> 정확한 Tool 단일 실행기 순서로 최대 네 단계를 실행합니다. Registry와 일치하지 않아도 capability catalog의 Tool만으로 해결 가능하면 inline 계획을 만들며 Agent의 자율 반복은 사용하지 않습니다.
- Workflow Orchestrator는 기본적으로 `datagov.agent_v4_workflow_skills`의 active Skill을 질문 기준 후보로 조회합니다. `inline_seed`는 사용자가 명시적으로 선택한 standalone 테스트 모드에서만 사용하며 MongoDB 오류 시 자동 fallback하지 않습니다.
- Workflow Orchestrator는 Loop 결과를 compact context로 만든 뒤 기본 Language Model을 한 번만 호출하며, 최종 `ChatOutput` 하나와 terminal `api_response` 하나를 제공합니다.
- HTML Visualization Flow는 `run_data_analysis`의 `result_ref`를 복원하고 외부 CDN 없는 standalone HTML/SVG 차트를 생성합니다. `HTML Report API 주소`로 게시해 Tauri 상대경로가 아닌 절대 보기·다운로드 링크를 반환하며, 화면 Message와 별도의 API 종료 어댑터가 실제 terminal `api_response`를 제공합니다. 그래프 요청은 `run_data_analysis -> run_visualization` 순서와 `handoff=result_ref`로 실행합니다.
- Metadata 및 Workflow Skill 저장 Flow 4종: Existing Loader를 Matcher에 직접 연결하고 단일 Writer/Response/Chat Output 사용
- Metadata 저장·조회 MongoDB 설정: 일반 노드 14개와 QA 통합 snapshot 노드 1개(컬렉션 3종)에 database/collection 기본값 명시
- Metadata 후보: 도메인 관련 항목 최대 10건, 테이블 최소 5/최대 10건, 메인 필터 전체, compact JSON 32KB 정책과 장비+UPH 질문 회귀 검증
- Data Analysis 파라미터: 각 retrieval job이 독립 실행 가능한 `required_params`를 가지며, 공통 조건은 각 job에 반복하고 `어제 재공과 오늘 생산량`처럼 범위가 다르면 서로 다른 값을 유지
- Metadata QA 제품 설명: 제품 그룹은 `product_terms`, 제품 집계는 `product_key_columns`와 관련 `analysis_recipes`만 근거로 결정론적 표를 만들고 추가 LLM 호출을 생략
