# metadata_driven_v5 구현 보고서

## 결론

v4의 큰 단계인 `의도 분석 -> 조회 -> pandas 분석 -> 답변`은 유지하되, 운영 비용과 감사 가능성에 직접 영향을 주는 경계를 다시 설계했습니다. v5는 Langflow standalone 배치를 전제로 하며, 제공된 Flow JSON에서 확인된 불필요한 payload 복제와 무조건 repair 호출을 제거했습니다.

## 구현 범위

- v4 custom component, prompt, docs, web/reference runtime을 독립 폴더로 복제
- 제공된 `1.1 data_analysis_flow_후속질문.json`을 v4 reference export로 보존
- v5 standalone Flow export와 재현 가능한 빌더 생성
- dummy 기본 경로 및 Oracle/H-API/Datalake/Goodocs live 노드 포함
- 신규/변경 계약에 대한 자동 테스트 추가
- 기존 디렉터리의 tracked 파일은 수정하지 않고 v5 폴더만 별도 생성

## 기존 방향을 유지한 부분

- Prompt/LLM 호출과 deterministic custom component를 분리
- intent plan을 먼저 만들고 source별 retriever로 라우팅
- pandas 코드를 LLM이 생성하되 제한된 executor에서 실행
- MongoDB result store와 compact follow-up state 사용
- 사용자 메시지와 구조화 API 응답을 별도 adapter/builder로 생성

이 골격은 새로 설계하더라도 유지할 만한 구조입니다. 문제는 단계 자체보다 각 단계 사이에서 전달하던 데이터의 범위와 실패 경로였습니다.

## 다르게 구현한 부분

| 영역 | v4/제공 Flow | v5 구현 |
| --- | --- | --- |
| Metadata prompt 입력 | loader 결과를 폭넓게 결합 | 도메인 관련 항목 최대 10건, 테이블 관련 후보 최소 5/최대 10건, 메인 필터 전체, compact JSON 32KB |
| Source 설정 | intent job에 source 설정 포함 가능 | LLM 출력에서 제거 후 active catalog로 hydration |
| 알 수 없는 dataset | downstream까지 전달 가능 | live는 차단, dummy만 명시적 fallback |
| Retrieval branch | main payload가 source 수만큼 복제 | job bundle + session/date context만 전달 |
| Helper 전달 | 약 11KB 전체 library를 pandas/repair prompt에 전달 | 선택된 함수 정의만 pandas prompt에 전달, 미선택 시 빈 문자열 |
| pandas 실패 경로 | 성공 여부와 무관하게 호출되는 repair Agent와 pass/repair 최종화 분기 | 단일 executor 내부에서 실제 오류일 때만 이전 코드·오류 문맥을 전달해 1회 복구하고, 하나의 최종 경로로 반환 |
| Row 소유 | data/analysis/answer_sections에 중복 | `data.rows` 단일 소유 |
| Code 소유 | analysis와 trace에 여러 alias | trace의 `generated_code` 단일 소유 |
| Message 소유 | answer/display/message alias | 최종 API `message` 단일 소유 |
| Session writer | 최종 응답과 병렬 side effect | Message/API의 공통 선행 노드 |
| Collection 기본값 | `agent_v4_*` | 최종 운영 결정에 따라 동일한 `agent_v4_*` 직접 공유 |
| 비-Router Chat Output | 분기별 복수 terminal 가능 | 모든 비-Router Flow가 Chat Output 1개 사용 |
| 저장 Flow 기존 문서 | loader 입력 미연결 | full document loader를 Matcher에만 연결하고 후보 누락분 exact lookup |
| 저장 Flow 중복 기본값 | 실제 pause/resume 없는 `ask` | `skip` 기본값, `ask` 제거 및 legacy 입력은 `skip` 정규화 |
| Metadata MongoDB UI 기본값 | 런타임 fallback은 있으나 일부 Flow 입력칸은 공백 | Metadata QA·저장 Flow 12개 노드에 `datagov`와 해당 `agent_v4_*` collection을 명시 |
| Router direct terminal | 예전 정상 Flow는 Smart Router에서 direct/clarification Chat Output으로 직접 연결 | 동일한 직접 연결로 복원하고 질문 JSON terminal을 만들던 별도 Gate 2개 제거 |
| 대안 Router | API Smart Router만 제공 | Agent + Tool Mode Router를 별도 추가하고 이름 기반 실제 Flow ID 해석·그래프 캐시·직접 결과 반환 적용 |

## 추가한 주요 컴포넌트

### 01D 질문 기반 메타데이터 후보 생성기

현재 질문을 기준으로 metadata를 점수화하고, 실제 후속 질문으로 판정된 경우에만 직전 질문과 compact state를 검색어에 보탭니다. 도메인은 관련 항목만 최대 10건, 테이블은 관련 후보 최소 5건·최대 10건, 메인 필터는 전체를 선택합니다. 한국어 조사와 `UPH를` 같은 영문 약어+조사를 정규화하고, 동점은 metadata key 기준으로 정렬해 MongoDB 반환 순서에 의존하지 않습니다. authoring trace와 source query 설정을 제거한 뒤 실제 LLM 전달 compact JSON에 32KB 제한을 적용합니다.

### 04A 신뢰 카탈로그 조회 작업 구성기

Intent LLM이 만든 job에서 source 관련 필드를 먼저 제거한 뒤 전체 active table catalog에서 실행 설정을 주입합니다. prompt용 후보가 축약되어도 실제 실행 설정은 전체 catalog loader 결과에서 가져옵니다.

### 15A 선택 helper 코드 생성기

Function case 선택 JSON을 읽고 Python AST 기준으로 필요한 top-level 함수 정의만 전체 library에서 추출합니다.

### 단일 pandas 실행 노드·조건부 1회 복구·최종화 경로

초기 pandas 실행이 성공하면 Repair LLM을 호출하지 않고 결과를 그대로 전달합니다. 실제 실행 오류가 나면 같은 executor가 최초 LLM 원본 코드, filter preamble이 적용된 실제 실행 코드, 오류 유형·메시지·축약 traceback, intent/filter plan, source schema·최대 5행 preview, 선택된 helper만 Repair LLM에 전달합니다. 수정 코드는 정확히 한 번만 재실행하며, 성공/최종 실패 모두 하나의 MongoDB 결과 저장·답변·세션·Message/API 경로로 전달합니다. 최종 Chat Output도 하나입니다.

최초 생성 Prompt와 Repair Prompt는 목적과 입력이 다릅니다. Repair 원문은 executor의 advanced 필드에 숨기지 않고 canvas의 `17B pandas 복구 프롬프트 템플릿` Text Input으로 분리했습니다. 실패 코드와 오류 문맥은 최초 실행 뒤에만 생기므로, raw 템플릿만 외부 노드에서 관리하고 실제 변수 치환·조건부 모델 호출은 executor 내부에서 지연 수행합니다.

LLM이 Prompt의 import 금지 지침을 간헐적으로 위반하는 경우를 위해 정확한 `import pandas as pd`, `import numpy as np`만 결정론적으로 제거합니다. 실제 import나 `__import__`는 허용하지 않으며, executor가 `pd`와 파일 I/O 기능이 없는 제한형 `np` 계산 namespace를 주입합니다. 그 외 import와 pandas/numpy 파일·네트워크 I/O attribute는 계속 차단합니다. 또한 Chat 진단에 Repair 시도·LLM 호출·선택 결과·최초/재시도/호출 오류를 표시해 Repair가 실행되지 않은 것처럼 보이던 문제를 보완했습니다.

### Metadata 저장 Flow 기존 문서 로더와 단일 Writer

Domain/Table Catalog/Main Flow Filter 저장 Flow는 Existing Loader의 full document를 Matcher에 직접 연결합니다. `registration_trace`는 제외하고, 로더 제한 밖 후보는 Matcher가 MongoDB에서 제한 조회합니다. Domain은 같은 section의 exact key와 유일한 normalized key/alias/display_name identity를 기존 canonical target으로 사용합니다. 따라서 `BG_PROCESS_GROUP` 후보가 `BG`, `B/G` 식별자를 공유하면 기존 `domain:process_groups:BG`를 교체하며, 유사 항목이 없으면 신규 저장합니다. 복수 target으로 모호하면 저장하지 않습니다. 기존 문서 목록은 Request나 LLM prompt에 전달하지 않습니다. dry/live 그래프 분기와 review Agent는 제거하고 결정론 검증을 수행하는 Writer 하나로 통합했습니다.

### API Router 중복 terminal 제거와 Agent Tool Router 추가

첫 번째 JSON 카드 문제는 `FinalGate` 두 개를 제거해 예전 Router와 같은 `SmartRouter.category_6/7 -> ChatOutput` 직접 연결로 복원했습니다. 이후 Message 형태로 질문이 두 번 반복된 실행을 DB에서 다시 확인한 결과, `Chat Input -> API caller.session_source` 5개 fan-out 때문에 ChatInput과 SmartRouter가 각각 2회 빌드되고 비선택 direct/clarification Chat Output이 질문을 저장한 것이 원인이었습니다. 해당 5개 edge를 제거해 Chat Input outgoing edge를 Smart Router 하나로 제한했습니다. Langflow가 각 caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 session 전달과 240초 timeout은 유지됩니다.

대안으로 `Agent + Tool Mode Router`를 별도 Flow로 추가했습니다. 표준 Run Flow는 export 시점 Flow ID가 import 후 유효하지 않고, Data Analysis의 편집용 Prompt/Helper/Repair Text Input까지 Tool schema에 포함할 수 있어 그대로 사용하지 않았습니다. 현재 LFX schema 기준 표준 5필드는 26,338 bytes였고 `ChatInput.input_value` 한 필드만 남긴 schema는 356 bytes입니다. 새 Tool은 대상 Flow 이름으로 현재 DB의 실제 ID와 `updated_at`을 조회한 뒤 Langflow shared graph cache를 사용합니다. Tool 5개 모두 `cache_flow=true`, `return_direct=true`이고 Agent는 `max_iterations=3`, `n_messages=6`, `add_current_date_tool=false`입니다. 질문은 Agent Tool 인자로 전달하고 session은 부모 `graph.session_id`에서 자동 상속하므로 `session_source` 포트와 fan-out edge는 없습니다. 최종 Chat Output은 하나입니다.

## 생성한 Flow export

`flow_exports/data_analysis_flow_v5_standalone.json`은 아래를 포함합니다.

- 41 nodes / 65 edges
- dummy 기본값
- 네 종류 live retriever와 merger 연결
- 신규 hydration/helper 노드와 단일 executor 내부 조건부 1회 pandas 복구 경로
- session writer를 거치는 최종 출력 경로
- 현재 폴더의 custom component source와 Intent/Pandas/Repair/Answer 4개 prompt source 내장

`tools/build_v5_data_analysis_flow.py`로 동일한 export를 다시 만들 수 있습니다. 원본 제공 JSON은 `flow_exports/data_analysis_flow_v4_reference.json`으로 보존했습니다.

## 검증 결과

- 전체 Python compile 성공
- 전체 pytest 221개 성공
- 대표 질문 23개 deterministic dummy 실행 23/23 성공
- 각 대표 질문에서 trusted catalog hydration, retrieval job/filter, pandas row/필수 컬럼, dummy 고지 message/API 계약 확인
- Flow export 재생성 결과와 저장된 JSON byte-level 구조 일치
- 41-node/65-edge DAG 확인
- 모든 embedded custom code/prompt가 현재 source 파일과 일치
- `langflow 1.8.2` / `lfx 0.3.4` 실제 runtime에서 현재 7개 Flow 115/115 node template parse/build 성공
- 별도 Dummy Flow 5종을 제거한 현재 bundle은 하위 Flow 5개, API Router, Agent Tool Router의 7개 JSON으로 구성
- 격리 Langflow 서버 `/api/v1/flows/upload/`에서 현재 7개 JSON 모두 HTTP 201 확인
- 격리 import에서 새 ID를 발급받은 Data Analysis Flow를 Agent Tool Router가 이름으로 해석하고, 별도 `session_source` 연결 없이 부모 실행 세션으로 `CachedFlowTool-data_analysis` partial build 성공
- 실제 v4 metadata export(도메인 63건, 테이블 9건, 메인 필터 17건)에서 `현재 D/A1 공정에 배정된 장비와 해당 모델의 UPH를 함께 보여줘` 질문이 `equipment_assign`과 `eqp_uph`를 모두 선택하고, 메인 필터 17건 전체 및 32KB 상한을 유지하는지 확인
- Data Analysis 내부 `retrieval_mode=dummy`로 대표 질문 23/23 확인
- 하위 Flow 5개와 Agent Tool Router에 Chat Output이 정확히 하나씩인지 확인
- 문제 실행 DB에서 User 1건, SmartRouter source 질문 Machine 2건, API caller source 최종 답변 1건과 하위 API 호출 1회를 확인하고, 반복 원인이 API 중복이 아닌 Router session fan-out 재빌드임을 확인
- Data Analysis에 pandas executor node와 최종화 경로가 하나씩이며, 성공 시 Repair LLM 0회·실패 시 최대 1회인지 확인
- Repair 성공 테스트에서 최초 생성 코드·실행 코드·오류/traceback 문맥이 프롬프트에 포함되고, 성공한 retry의 active error가 비워지는지 확인
- Repair 재실패 테스트에서 추가 LLM 호출 없이 최종 retry 오류와 최초 오류 감사 정보가 모두 보존되는지 확인
- 사용자 HOLD 코드 형태의 `import pandas as pd`가 Repair 호출 없이 정규화되어 성공하고, 정확한 numpy alias는 제한형 `np.where`로 실행되며, 기타 import와 pandas/numpy 파일 I/O는 계속 차단되는지 확인
- Chat 진단에서 Repair 시도·LLM 호출·선택 결과·최초/재시도/호출 오류가 구분되어 표시되는지 확인
- Metadata 저장 Flow 3종의 Existing Loader -> Matcher 및 단일 Writer/Response/Chat Output 연결 확인
- Domain `replace`에서 `BG_PROCESS_GROUP` 후보가 `BG/B/G` identity로 기존 `domain:process_groups:BG`를 교체하고, 유사 항목이 없으면 신규 저장하며, 복수 target이면 0건 저장하는 회귀 확인
- helper library의 prompt direct edge 제거 확인
- response builder에서 Message/API로 가는 direct edge 제거 및 session writer 선행 확인

### 실제 Langflow 검증에서 발견해 수정한 호환 문제

1. `15A 선택 helper 코드 생성기`의 `MessageTextInput(multiline=True)`는 `lfx 0.3.4` 스키마에서 허용되지 않았습니다. multiline 옵션을 제거한 뒤 template build를 재검증했습니다.
2. v4 export에 직렬화돼 있던 `MONGO_URL`/`MONGO_URL_GEN` Global Variable 참조가 standalone 서버에서 코드의 환경변수 fallback보다 먼저 실패했습니다. v5 export builder는 `mongo_uri`를 빈 값/`load_from_db=false`로 정규화했습니다. 초기에는 별도 v5 collection 기본값을 적용했으나, 최종 운영 결정에 따라 `agent_v4_*`를 직접 공유하도록 변경했으며 데이터 복사나 migration은 하지 않습니다.
3. Langflow 1.8.2 frontend는 타입과 handle이 일치해도 `advanced=true` 입력에 연결된 edge를 제거합니다. `15A -> 17` 선택 helper, 저장 Flow 3종의 Existing Loader -> Matcher, API Router 5개 session source 입력을 모두 일반 연결 포트로 변경했고, bundle 생성기가 connected advanced input을 발견하면 실패하도록 검증을 추가했습니다.

호환 수정과 Router 정리 후 현재 export 기준 전체 bundle 115/115 template build를 다시 실행해 전부 통과했습니다.

## 운영 전 남은 확인

1. 사용 모델/provider를 회사 표준 모델로 교체하고 timeout/retry 정책 확정
2. `agent_v4_*` collection 접근 권한과 환경변수 연결 확인. 별도 seed 업로드나 v5용 migration은 수행하지 않음
3. source별 live smoke test
4. 대표 질문과 후속질문 2-turn 검증
5. MongoDB TTL/index 권한 및 세션 저장 실패 처리 정책 확정

실데이터 접근과 격리 서버의 `GOOGLE_API_KEY`가 없는 현재 환경에서는 Agent/LLM 전체 호출과 4, 5의 live 구간을 검증하지 않았습니다. 따라서 v5를 “운영 완료”가 아니라 “standalone import/runtime 호환 및 dummy 계약 검증 완료” 상태로 판단합니다.
