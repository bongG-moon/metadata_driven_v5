# Environment Setup

이 프로젝트는 실제 MongoDB/Gemini 검증으로 확장할 수 있도록 `.env` 파일을 사용한다.

## Files

- `.env.example`: 공유 가능한 템플릿
- `.env`: 로컬에서 실제 값을 채우는 파일

`.env`에는 비밀값이 들어갈 수 있으므로 `.gitignore`에 포함되어 있다.

## Required For MongoDB Validation

```dotenv
MONGODB_URI=<set-in-local-env>
MONGODB_DATABASE=datagov
MONGODB_DOMAIN_COLLECTION=agent_v4_domain_items
MONGODB_TABLE_CATALOG_COLLECTION=agent_v4_table_catalog_items
MONGODB_MAIN_FLOW_FILTER_COLLECTION=agent_v4_main_flow_filters
MONGODB_RESULT_COLLECTION=agent_v4_result_store
MONGODB_WORKFLOW_SKILL_COLLECTION=agent_v4_workflow_skills
RUN_MONGODB_VALIDATION=true
```

v5는 v4와 같은 `datagov` 데이터베이스 및 위 full collection name을 직접 공유한다. v5 전용 컬렉션으로 데이터를 복사하거나 별도 migration을 수행하지 않는다.

## Required For Gemini LLM Validation

```dotenv
LLM_PROVIDER=gemini
LLM_API_KEY=...
LLM_MODEL_NAME=...
LLM_TEMPERATURE=0
RUN_LLM_VALIDATION=true
```

이전 rebuild 폴더와 동일하게 Python 검증은 `langchain_google_genai.ChatGoogleGenerativeAI`를 사용한다.
`LLM_MODEL_NAME`은 운영자가 실제 사용 가능한 Gemini 모델 이름으로 채운다.
로컬 도구가 Google 표준 이름을 요구하면 `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY`에도 같은 값을 넣을 수 있다.

## Required For Langflow Desktop/Web API Validation

Langflow Desktop에서 각 flow를 만든 뒤, flow 우측 상단의 API/Share/Run API 화면에서 flow id를 확인한다.
full URL이 `http://127.0.0.1:7860/api/v1/run/3023...` 형태라면 `LANGFLOW_BASE_URL`에는 `http://127.0.0.1:7860`만 넣고, 각 `*_FLOW_ID`에는 마지막 UUID만 넣으면 된다.
full URL을 그대로 쓰고 싶으면 대응되는 `*_API_URL`에 전체 주소를 넣어도 된다.
Streamlit web app은 repo 루트 또는 현재 실행 폴더의 `.env`를 자동으로 읽는다. 이미 OS 환경변수로 설정된 값은 `.env`가 덮어쓰지 않는다.

```dotenv
LANGFLOW_BASE_URL=http://127.0.0.1:7860
LANGFLOW_API_KEY=
LANGFLOW_INPUT_TYPE=chat
LANGFLOW_OUTPUT_TYPE=chat
LANGFLOW_TIMEOUT_SECONDS=300

LANGFLOW_ROUTER_FLOW_ID=
LANGFLOW_METADATA_QA_FLOW_ID=
LANGFLOW_DATA_ANALYSIS_FLOW_ID=

LANGFLOW_DOMAIN_SAVING_FLOW_ID=
LANGFLOW_TABLE_CATALOG_SAVING_FLOW_ID=
LANGFLOW_MAIN_FILTER_SAVING_FLOW_ID=

RUN_LANGFLOW_API_VALIDATION=true
```

검증 기준은 다음과 같다.

- router flow: web app의 첫 진입점이다.
- metadata/data flow: router flow 내부에서 Smart Router output이 route별 API caller로 전달되고, 선택된 하위 Flow의 `/api/v1/run/...` endpoint를 한 번 호출한다. Desktop/Web 검증용 `LANGFLOW_ROUTER_FLOW_ID`는 Smart Router와 5개 API caller 연결이 포함된 현재 v5 router flow를 가리켜야 한다.
- saving flow: web metadata 관리 화면에서 신규 metadata를 등록할 때 사용한다.
- `LANGFLOW_INPUT_TYPE`은 현재 컴포넌트들이 `MessageTextInput` 기반이면 `chat`을 기본으로 둔다. 실제 Langflow API 화면에서 `input_type`을 `text`로 안내하면 `.env`에서 `text`로 바꾸면 된다.

Router 내부의 하위 Flow read timeout은 240초이고, 외부 Web/API client는 Smart Router 판단과 응답 직렬화 여유를 포함해 300초를 기본으로 사용한다. 300초 상향은 timeout 오판을 줄이는 설정이며 Flow 자체의 실행시간을 단축하지는 않는다.

### Langflow Desktop Trace/Vertex 저장 설정

저장소 루트의 `.env`는 Streamlit web app이 읽는다. Langflow Desktop backend 설정은 다음 별도 파일에 입력한다.

```text
%APPDATA%\com.LangflowDesktop\data\.env
```

```dotenv
LANGFLOW_NATIVE_TRACING=false
LANGFLOW_VERTEX_BUILDS_STORAGE_ENABLED=false
```

- `LANGFLOW_NATIVE_TRACING=false`: 신규 `trace`/`span` DB 저장을 중지한다.
- `LANGFLOW_VERTEX_BUILDS_STORAGE_ENABLED=false`: Playground/API build의 vertex output 저장을 중지한다.
- 두 설정 모두 Flow 실행 결과에는 영향을 주지 않지만 Trace View와 과거 vertex output 확인 범위가 줄어든다.
- 설정 후 Langflow Desktop을 재시작해야 한다.
- 전체 tracing provider까지 끄는 `LANGFLOW_DEACTIVATE_TRACING=true`보다 native DB 저장만 끄는 위 설정을 우선한다.
- Langflow 1.8.2에는 trace 보존 기간 자동 제한이 없으므로 tracing을 유지한다면 별도 retention 정리가 필요하다.

운영 기본 API Router에는 Run Flow 노드가 없습니다. 별도로 제공되는 `07_agent_tool_router_flow_v5_standalone.json`에는 이름 기반 Cached Run Flow Tool 5개, `08_workflow_orchestrator_flow_v5_standalone.json`에는 HTML 시각화를 포함한 6개가 있으며 모두 `Cache Flow=true`입니다. 이 설정은 하위 Flow 그래프만 캐시하고 데이터 조회·pandas·LLM 결과는 캐시하지 않습니다.

08 Workflow Orchestrator는 기본 Agent 대신 계획용 Language Model, Langflow 기본 Loop, 최종 합성용 Language Model을 사용합니다. 운영 기본값은 `MONGO_URL`로 `datagov.agent_v4_workflow_skills`의 active Skill을 조회하는 `mongodb` 모드이며, 현재 질문과 관련된 후보만 최대 8개·64KB로 제한합니다. `inline_seed`는 명시적 로컬 테스트 모드이고 MongoDB 오류 시 자동 fallback하지 않습니다. 단계는 최대 4개를 순차 호출하며, `handoff=result_ref`가 있는 업무는 `datagov.agent_v4_result_store`, 부모·자식의 동일 `session_id`가 필요합니다. 하위 Flow 실행 시간을 합산해야 하므로 외부 client timeout은 Workflow의 최장 예상 시간보다 길게 설정합니다.

10 HTML Visualization Flow도 같은 `MONGO_URL`, `datagov`, `agent_v4_result_store`를 사용합니다. 08이 `run_data_analysis` 다음 `run_visualization`을 실행할 때 분석 결과 `result_ref`를 그대로 전달하며, 차트 HTML은 외부 CDN 없이 생성합니다. 보기·다운로드 링크는 별도 `report_api` 서버가 발급하므로 Desktop에서는 `http://127.0.0.1:8010`, Kubernetes에서는 Langflow가 호출 가능한 Service 주소와 사용자 브라우저가 접근 가능한 `BASE_URL`을 각각 설정해야 합니다. 자세한 내용은 `docs/HTML_REPORT_LINK_GUIDE.md`를 참고합니다.

09 Workflow Skill 저장 Flow는 같은 `MONGO_URL`, `datagov`, `agent_v4_workflow_skills`를 기존 항목 로더·유사 항목 조회기·Writer에 입력합니다. `dry_run=true`가 기본이며 실제 저장 때만 끕니다. 저장 후 08 Workflow Orchestrator는 다음 요청에서 MongoDB를 다시 조회하므로 Flow JSON을 재export할 필요가 없습니다.

### 동일 질문 호출 경로 벤치마크

`tools/benchmark_langflow_call_paths.py`는 같은 질문을 각 경로에 기본 5회씩 호출하고 min/P50/P95/max 및 실패 수를 JSON으로 저장한다. 비교 오염을 줄이기 위해 기본값은 요청마다 새 session ID를 사용하고, 호출 순서는 라운드마다 반대로 바꾼다.

```powershell
python tools\benchmark_langflow_call_paths.py `
  --label tracing_off `
  --question "오늘 DA공정 생산량 알려줘" `
  --direct-url "http://127.0.0.1:7860/api/v1/run/<DATA_ANALYSIS_ENDPOINT>" `
  --router-url "http://127.0.0.1:7860/api/v1/run/<ROUTER_ENDPOINT>" `
  --repeats 5 `
  --timeout-seconds 300
```

Agent Tool Router도 비교하려면 `--run-flow-url "http://127.0.0.1:7860/api/v1/run/<AGENT_TOOL_ROUTER_ENDPOINT>"`를 추가합니다. 이 경로는 첫 실행과 warm cache 실행을 구분해 해석해야 합니다. tracing/vertex 저장 on/off 비교는 Desktop backend `.env` 변경 후 재시작하고 `--label tracing_on`, `--label tracing_off` 결과를 각각 남깁니다.

## Optional Source Retrieval Settings

기본 검증은 실제 Oracle/H-API/Datalake/Goodocs를 호출하지 않고 deterministic dummy data를 사용한다.
이때도 `table_catalog.json`의 `source_type` 경계는 유지되므로 Langflow 연결과 pandas 분석 scope를 검증할 수 있다.

```dotenv
ORACLE_CONFIG_JSON=
H_API_TOKEN=
LAKEHOUSE_USER_ID=
LAKEHOUSE_TOKEN=
LAKEHOUSE_S3_ACCESS_KEY=
LAKEHOUSE_S3_SECRET_KEY=
GOODOCS_USER_ID=
GOODOCS_TOKEN_SOURCE=
GOODOCS_TOKEN_KEY=
SOURCE_FETCH_LIMIT=5000
```

실제 source connector를 시도하려면 import한 Data Analysis Flow의 `04A.retrieval_mode`를 `live`로 바꾸고 위 credential/config 값을 채운다. dummy/live 모드는 환경변수와 `07`에서 중복 설정하지 않는다.
자세한 source별 역할은 `docs/DATA_RETRIEVAL_SOURCES.md`를 참고하면 된다.

## Check Environment

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python tools\validate_env.py
```

Gemini API 호출까지 확인하려면:

```powershell
python tools\validate_gemini_connection.py
```

## Upload JSON Seed To MongoDB

```powershell
python tools\upload_json_to_mongodb.py --dry-run
python tools\upload_json_to_mongodb.py
```

부분 업로드가 필요하면 `--metadata-kind`를 사용합니다.

```powershell
python tools\upload_json_to_mongodb.py --dry-run --metadata-kind domain
python tools\upload_json_to_mongodb.py --metadata-kind table-catalog
python tools\upload_json_to_mongodb.py --metadata-kind main-flow-filter
python tools\upload_json_to_mongodb.py --metadata-kind table-catalog,main-flow-filter
```

`tools/upload_json_to_mongodb.py`는 실행 시 `.env`를 자동으로 읽는다. CLI 옵션이 `.env`보다 우선한다.
