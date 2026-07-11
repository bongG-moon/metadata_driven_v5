# Web Implementation Guide

이 문서는 `metadata_driven_v5`의 standalone Langflow 구성 기준으로 API Router, query flow, Metadata QA, metadata saving flow를 web에서 안전하게 사용하기 위한 구현 가이드다.

## 0. Current Runtime Direction

신규 운영 구조는 단일 `main_flow`가 아니라 아래처럼 분리한다.

```text
Web/API
-> route_flow (06 API Router)
-> Smart Router route output
-> route별 Langflow Run API caller
-> metadata_qa_flow | data_analysis_flow | metadata saving flows
```

combined `main_flow` canvas는 사용하지 않는다. web backend는 Router Flow 하나를 호출하고 Router는 선택된 branch에서 하위 Flow의 `/api/v1/run/{flow_id}`를 호출한다.

대상 사용자는 Langflow, Python, JSON, MongoDB를 직접 다루지 않는 현업 작업자다. Web은 Langflow canvas 편집기를 노출하는 도구가 아니라, 자연어 질의, metadata 등록, 검토, 검증을 업무 화면으로 감싸는 얇은 운영 레이어여야 한다.

기준이 되는 현재 flow 문서:

- `langflow_components/route_flow/CONNECTION_GUIDE.md`
- `langflow_components/route_flow_v2/CONNECTION_GUIDE.md`
- `langflow_components/metadata_qa_flow/CONNECTION_GUIDE.md`
- `langflow_components/data_analysis_flow/CONNECTION_GUIDE.md`
- `langflow_components/domain_saving_flow/CONNECTION_GUIDE.md`
- `langflow_components/table_catalog_saving_flow/CONNECTION_GUIDE.md`
- `langflow_components/main_flow_filters_saving_flow/CONNECTION_GUIDE.md`
- `docs/METADATA_SAVING_FLOW_GUIDE.md`
- `docs/DATA_RETRIEVAL_SOURCES.md`

## 1. 구현 범위

Web에서 제공해야 하는 기능은 네 가지다.

1. 제조 데이터 질의/분석 실행
2. 자연어 기반 metadata 등록
3. 기존 metadata 조회/검토
4. 등록 후 main flow 검증 질문 실행

Web에서 일반 사용자에게 제공하지 않을 기능:

- Langflow node/canvas 편집
- raw MongoDB write/edit
- source credential, MongoDB URI, Langflow API key 노출
- main agent prompt 또는 pandas code 직접 수정
- metadata collection과 result row collection의 직접 조작

## 2. 현재 Flow 구조

권장 운영 구조:

```text
Browser UI
-> Web Backend API
-> Langflow Run API
-> Langflow router/subflows/saving flows
-> MongoDB metadata collections and result store
```

Query runtime의 운영 기본은 API Router split mode입니다. Web backend는 Router Flow만 호출하고, Router 내부의 Smart Router branch가 하위 Flow Run API를 호출합니다. branch별 URL은 bundle의 고정 `endpoint_name`으로 이미 설정됩니다. 비교 환경에서는 Agent + Tool Mode Router endpoint를 같은 API 계약으로 호출할 수 있습니다.

현재 `metadata_driven_v5`의 flow는 아래 기능 묶음으로 본다.

| Flow | 폴더 | Web 관점의 역할 |
| --- | --- | --- |
| Query router (06) | `langflow_components/route_flow/` | 질문 유형을 분류하고 선택 branch에서 하위 Flow Run API 호출 |
| Agent Tool router (07) | `langflow_components/route_flow_v2/` | Agent가 이름 기반 cached Flow Tool 5개 중 하나를 선택하고 단일 Chat Output 반환 |
| Metadata QA | `langflow_components/metadata_qa_flow/` | metadata 질문에 직접 답변 |
| Data analysis | `langflow_components/data_analysis_flow/` | 질문, state, metadata를 받아 retrieval, pandas, 답변, next state 생성 |
| Data retrieval | `langflow_components/data_analysis_flow/07~12_*` | `source_type`별 조회 결과를 `retrieval_payload`로 병합 |
| Domain saving | `langflow_components/domain_saving_flow/` | domain 용어/규칙을 자연어에서 MongoDB item으로 저장 |
| Table catalog saving | `langflow_components/table_catalog_saving_flow/` | dataset/source/column/filter mapping을 자연어에서 저장 |
| Main flow filter saving | `langflow_components/main_flow_filters_saving_flow/` | 날짜, 공정, 제품, LOT, 장비 등 filter metadata 저장 |

별도 Dummy Flow는 제거했습니다. 실제 데이터 연결 전 검증은 Data Analysis Flow 내부의 `04A.retrieval_mode=dummy`를 사용합니다.

Web backend가 담당해야 하는 일:

- Langflow flow id와 API key 관리
- MongoDB URI, LLM key, source credential 같은 secret 관리
- 사용자 session과 main flow `state` 저장
- 승인 대기 UI를 별도로 운영하는 경우에만 saving 중복 선택용 pending record 저장. v5 Langflow 자체에는 `ask`/pause-resume 계약이 없음
- Langflow 응답을 화면용 response로 정리
- 큰 result row가 `data_ref`로 축약된 경우 필요 시 backend에서 재조회

Browser가 직접 하지 말아야 하는 일:

- MongoDB URI, source credential, Langflow admin token 보관
- MongoDB collection 직접 write
- Langflow 내부 payload 전체 표시
- `runtime_sources`, full prompt, raw source rows, secret 포함 config 표시

## 3. 현재 Query Flow 계약

현재 query runtime은 router/subflow 연결 순서를 따른다. Web/API 검증에서는 router flow 내부에서 선택된 flow 실행까지 끝내는 구성을 기본으로 둔다.

```text
Web/API
-> route_flow (06 API Router)
-> Smart Router route output
-> route별 고정 API caller 실행
-> router flow final message/API response
```

Web/API backend는 Router Flow 하나만 호출한다. Router 안에서 Smart Router가 route를 나누고 각 route output은 대상 Flow URL이 설정된 API caller로 연결된다. `LANGFLOW_ROUTER_FLOW_ID` 또는 `LANGFLOW_ROUTER_API_URL`은 하위 API 호출과 최종 Message 반환까지 포함된 `api_router_flow_v5_standalone.json` import Flow를 가리켜야 한다.

`data_analysis_flow`가 선택된 경우 subflow 내부 순서는 다음과 같다.

```text
00 MongoDB Session State Loader
-> 00 Analysis Request Loader
-> 01E Follow-up Hint Builder
-> 01A/01B/01C MongoDB Metadata Loaders
-> 01D Metadata Candidate Builder
-> 02 Intent Variables / 03 Intent Prompt
-> Intent LLM
-> 04 Intent Plan Normalizer
-> 04A Trusted Catalog Retrieval Job Builder
-> 05 Previous Result Loader
-> 06 Retrieval Job Validator
-> 07 Retrieval Job Router
-> 08 dummy or 09~12 live source retrievers
-> 13 Source Retrieval Merger
-> 14 Retrieval Payload Adapter
-> 15 Pandas Variables / 15A Selected Helper
-> 16 Pandas Prompt
-> Pandas Code LLM
-> 17 Pandas Executor with error-only one-shot repair
-> 23 MongoDB Result Store
-> 18 Answer Variables / 19 Answer Prompt
-> Answer LLM
-> 20 Answer Response Builder
-> 01 MongoDB Session State Writer
-> 21 Message Adapter / 22 API Response Builder
-> Chat Output
```

Web에서 중요한 차이:

- `23 MongoDB Result Store`가 pandas 직후 `runtime_sources` 원본 rows와 `result_rows` 전체 결과 rows를 `MONGODB_RESULT_COLLECTION`에 저장하고, runtime/API payload에는 preview row와 `data_ref`만 남긴다.
- metadata QA flow가 `direct_response_ready=true`를 만든 질문은 데이터 retrieval/pandas 저장 대상이 아니다.
- `metadata_route.target_dataset`은 dataset 설명/쿼리/활용 예시 같은 metadata QA에서만 쓰는 대상 포인터다. 일반 분석 질문에서 조회할 dataset 목록은 intent plan이 별도로 결정한다.
- 사용자는 dataset key를 몰라도 된다. `생산량 데이터 조회 쿼리 알려줘` 같은 표현은 `domain_items.quantity_terms`의 alias와 dataset mapping을 통해 대표 dataset으로 해석한다.
- `20 Answer Response Builder`의 `payload_out`이 답변, 표, 적용 scope, next `state`를 만들며, `analysis.data_ref`를 `data.data_ref`와 `state.current_data.data_ref`로 이어받는다.
- `21 Answer Message Adapter`는 Langflow Playground/Chat Output용 Markdown message를 만든다. Web에서도 같은 표시 형식을 쓰려면 `21.message`를 `22 API Response Builder.display_message`에 연결한다.
- `22 API Response Builder`는 표준 JSON 응답을 만들며, `display_message`가 연결된 경우 `message/display_message`에 21번 markdown을 포함하고 `answer_message`에는 LLM의 원문 답변을 유지한다.
- Web backend는 선택된 subflow의 API response output을 표준 query response로 사용한다.
- Langflow Run API가 text/message 포트만 반환하는 운영 방식이면 API message의 JSON 문자열을 파싱한다.
- `12` node는 새 비즈니스 로직을 만들지 않고 `10.payload_out`의 projection만 수행한다.

Web/API query 입력:

```json
{
  "question": "오늘 DA, WB공정에서 각각 재공 상위 3개 제품을 뽑아줘",
  "session_id": "user-session-id"
}
```

Report/diagnosis route를 검증할 때는 후속형 문장보다 E2E 업무 요청 문장을 우선 사용한다.

```text
오늘 WB공정 기준으로 생산량, 재공, 목표 달성률을 포함한 요약 리포트 만들어줘
오늘 HBM 제품군 생산 저조 원인을 장비, 재공, HOLD LOT 관점으로 진단해줘
```

선택된 subflow의 `00 ... Request Loader`가 만드는 내부 request payload:

```json
{
  "payload_version": "agent-v1",
  "status": "ok",
  "request": {
    "session_id": "user-session-id",
    "question": "오늘 DA, WB공정에서 각각 재공 상위 3개 제품을 뽑아줘",
    "reference_date": "2026-07-01"
  },
  "state": {
    "chat_history": [],
    "context": {},
    "current_data": {}
  },
  "warnings": [],
  "errors": []
}
```

Web에서 노출할 main response shape:

```json
{
  "status": "ok",
  "answer_message": "...",
  "data": {
    "columns": [],
    "rows": [],
    "row_count": 0,
    "data_ref": {
      "store": "mongodb",
      "collection_name": "agent_v4_result_store",
      "ref_id": "..."
    }
  },
  "applied_scope": {
    "intent_type": "multi_step_analysis",
    "analysis_kind": "rank_wip_then_join_production",
    "datasets": ["wip_today", "production_today"],
    "source_aliases": [],
    "filters_by_source": {},
    "params_by_source": {},
    "metadata_refs": {}
  },
  "intent_plan": {},
  "analysis": {
    "status": "ok",
    "safety_passed": true,
    "executed": true,
    "columns": [],
    "rows": [],
    "row_count": 0,
    "analysis_code": "",
    "errors": []
  },
  "state": {},
  "warnings": [],
  "errors": []
}
```

주의:

- `data.rows`는 전체 row가 아니라 preview일 수 있다. `row_count`와 `data_ref`를 함께 봐야 한다.
- 전체 row 다운로드/상세 보기가 필요하면 backend가 `MONGODB_RESULT_COLLECTION`에서 `data_ref.ref_id`로 조회하는 별도 endpoint를 둔다.
- 후속 질문을 위해 backend는 반환된 `state`를 session별로 저장한다.
- 다음 query 실행 시 저장된 compact `state`는 선택된 subflow 내부의 session state loader가 복원한다.
- 이전 결과 전체 rows가 필요한 follow-up이면 `data_analysis_flow`의 `04 Previous Result Restore Router`가 `05 MongoDB Data Loader` branch를 실행한다.

## 4. 현재 Metadata Saving Flow 계약

세 saving flow는 같은 패턴을 사용한다.

```text
00 Request Loader
-> 03 Saving Variables Builder
-> Langflow Prompt Template
-> Gemini/LLM saving JSON 1회
-> 04 Saving Result Normalizer
00 Existing Items Loader ------------------+
-> 05 Similarity Checker
-> 07 Deterministic Review/Writer
-> 08 Saving Response Normalizer
-> 09 Saving Message Adapter
-> 10 Saving API Response Builder
-> Chat Output 1개
```

현재 saving flow의 표준 web/API 응답은 각 flow의 `10 ... Saving API Response Builder.api_response`다.
`09 ... Saving Message Adapter.message`는 Chat Output/Playground 표시용 Markdown이다.
Web은 `09.message`를 JSON 계약으로 파싱하지 않는다.

Saving flow 입력:

```json
{
  "raw_text": "W/B공정은 W/B1부터 W/B6까지야. 재공 수량은 WIP 컬럼을 합산해.",
  "duplicate_action": "skip"
}
```

Backend가 flow에 전달해야 하는 설정:

| Metadata type | Flow id | collection name input |
| --- | --- | --- |
| `domain` | `LANGFLOW_DOMAIN_SAVING_FLOW_ID` | `agent_v4_domain_items` |
| `table_catalog` | `LANGFLOW_TABLE_CATALOG_SAVING_FLOW_ID` | `agent_v4_table_catalog_items` |
| `main_flow_filter` | `LANGFLOW_MAIN_FILTER_SAVING_FLOW_ID` | `agent_v4_main_flow_filters` |

중요한 현재 기준:

- collection은 prefix 조합이 아니라 full collection name을 직접 넘긴다.
- `collection_prefix`는 legacy fallback으로만 생각하고 새 web 구현의 기본값으로 쓰지 않는다.
- `duplicate_action`은 `skip`, `merge`, `replace`, `create_new` 중 하나이며 기본값은 `skip`이다.
- Existing Loader, Similarity Checker, Writer에는 동일한 Mongo database/collection 설정을 전달한다.
- `ask`는 실제 pause/resume HITL이 아니므로 v5 Flow 계약에 없다.

`10 ... API Response Builder.api_response` shape:

```json
{
  "response_type": "metadata_authoring",
  "status": "saved | skipped | dry_run | needs_input | error | not_saved",
  "success": true,
  "direct_response_ready": true,
  "message": "사용자 표시 메시지",
  "display_message": "Playground/Chat 표시용 Markdown",
  "answer_message": "요약 문장",
  "metadata_type": "domain | table_catalog | main_flow_filter",
  "metadata_label": "도메인 | 테이블 카탈로그 | 메인 플로우 필터",
  "answer_sections": {},
  "data": {"columns": [], "rows": [], "row_count": 0},
  "metadata_authoring": {},
  "write_result": {
    "success": true,
    "dry_run": false,
    "saved_count": 0,
    "would_save_count": 0,
    "skipped_count": 0
  },
  "trace": {}
}
```

UI status는 다음처럼 해석한다.

| 조건 | Web 표시 상태 |
| --- | --- |
| `status == "saved"` 또는 `write_result.saved_count > 0` | 저장 완료 |
| `status == "dry_run"` 또는 `write_result.dry_run == true` | 저장 전 검토 |
| `status == "skipped"` 또는 `write_result.skipped_count > 0` and `saved_count == 0` | 기존 항목 유지 / 중복 쓰기 생략 |
| `status == "needs_input"` | 추가 정보 필요 |
| `conflict_warnings`만 있음 | 저장 결과와 함께 경고 표시 |
| `write_result.status == "error"` 또는 `errors` 있음 | 오류 |

`existing_matches`가 있고 `duplicate_action=skip`이면 `skipped_count`와 `operation_by_key`를 기준으로 기존 문서를 유지했음을 표시한다. `merge`, `replace`, `create_new`는 사용자가 실행 전에 명시적으로 선택한 경우에만 사용한다.

## 5. Metadata 저장 Schema

### 5.1 Domain

Collection: `agent_v4_domain_items`

Writer key: `section + key`

Main flow load 위치: `metadata.domain_items`

```json
{
  "_id": "domain:process_groups:DA",
  "section": "process_groups",
  "key": "DA",
  "status": "active",
  "payload": {
    "display_name": "D/A",
    "aliases": ["DA", "D/A"],
    "processes": ["D/A1", "D/A2"]
  }
}
```

허용 section 예:

- `process_groups`
- `product_terms`
- `quantity_terms`
- `metric_terms`
- `status_terms`
- `product_key_columns`

Saving normalizer는 `count_distinct` 집계를 main/runtime에서 쓰기 쉬운 `nunique`로 정규화한다.

### 5.2 Table Catalog

Collection: `agent_v4_table_catalog_items`

Writer key: `dataset_key`

Main flow load 위치: `metadata.table_catalog.datasets`

```json
{
  "_id": "table_catalog:wip_today",
  "dataset_key": "wip_today",
  "status": "active",
  "payload": {
    "display_name": "WIP Today",
    "dataset_family": "wip",
    "date_scope": "current_day",
    "source_type": "oracle",
    "source_config": {
      "source_type": "oracle",
      "db_key": "PNT_RPT",
      "query_template": "SELECT WORK_DT, OPER_NAME, WIP FROM PKG_WIP_TODAY WHERE WORK_DT = {DATE}"
    },
    "required_params": ["DATE"],
    "required_param_mappings": {"DATE": ["WORK_DT"]},
    "filter_mappings": {"DATE": ["WORK_DT"], "OPER_NAME": ["OPER_NAME"]},
    "columns": ["WORK_DT", "OPER_NAME", "WIP"],
    "primary_quantity_column": "WIP"
  }
}
```

`source_type`별 최소 저장 정보:

| source_type | Required source_config |
| --- | --- |
| `oracle` | `db_key`, `query_template` |
| `h_api` | `api_url` |
| `datalake` | `query_template` |
| `goodocs` | `doc_id`, `sheet_name` |
| `dummy` | 운영용이 아니면 최소 `columns`, `dataset_family` |

주의:

- SQL은 source 조회에 필요한 `query_template`으로만 저장한다. DB password/token은 절대 metadata에 저장하지 않는다.
- `filter_mappings`의 key는 main flow filter key이고, value는 실제 dataset column 후보 목록이다.
- `DATE` 같은 filter key가 실제 column 목록에 없다는 이유만으로 막으면 안 된다. 실제 column은 value 쪽 `WORK_DT` 같은 이름이다.

### 5.3 Main Flow Filter

Collection: `agent_v4_main_flow_filters`

Writer key: `filter_key`

Main flow load 위치: `metadata.main_flow_filters`

```json
{
  "_id": "main_flow_filter:DATE",
  "filter_key": "DATE",
  "status": "active",
  "payload": {
    "display_name": "기준일",
    "aliases": ["오늘", "금일", "작업일"],
    "column_candidates": ["WORK_DT", "DATE", "BASE_DT"],
    "semantic_role": "date",
    "value_type": "date",
    "value_shape": "scalar",
    "operator": "eq",
    "normalized_format": "YYYYMMDD"
  }
}
```

저장 가능한 filter item의 최소 정보:

- `filter_key`
- `payload.aliases`
- `payload.column_candidates`
- `payload.semantic_role`

`value_type`, `value_shape`, `operator`가 누락되면 saving normalizer가 보수적으로 `string`, `scalar`, `eq`를 기본값으로 넣는다.

## 6. 필요한 환경 변수

Web backend와 Langflow 운영 기준으로 아래 설정을 분리한다.

```env
# Langflow backend wrapper
LANGFLOW_BASE_URL=http://127.0.0.1:7860
LANGFLOW_API_KEY=
LANGFLOW_ROUTER_FLOW_ID=
LANGFLOW_DATA_ANALYSIS_FLOW_ID=
LANGFLOW_METADATA_QA_FLOW_ID=
LANGFLOW_REPORT_GENERATION_FLOW_ID=
LANGFLOW_OPERATIONS_DIAGNOSIS_FLOW_ID=
LANGFLOW_DOMAIN_SAVING_FLOW_ID=
LANGFLOW_TABLE_CATALOG_SAVING_FLOW_ID=
LANGFLOW_MAIN_FILTER_SAVING_FLOW_ID=

# LLM
LLM_PROVIDER=gemini
LLM_API_KEY=
LLM_MODEL_NAME=
LLM_TEMPERATURE=0
LLM_TIMEOUT_SECONDS=60

# MongoDB metadata and result store
MONGODB_URI=
MONGODB_DATABASE=datagov
MONGODB_DOMAIN_COLLECTION=agent_v4_domain_items
MONGODB_TABLE_CATALOG_COLLECTION=agent_v4_table_catalog_items
MONGODB_MAIN_FLOW_FILTER_COLLECTION=agent_v4_main_flow_filters
MONGODB_RESULT_COLLECTION=agent_v4_result_store

# Source retrieval
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

# Web-owned storage
WEB_SESSION_STORE=mongodb
WEB_PENDING_SAVING_COLLECTION=agent_v4_pending_saving
```

v5 web/Flow는 위 v4 collection을 직접 공유한다. v5 전용 MongoDB collection으로 데이터를 복사하거나 migration하지 않는다. `WEB_PENDING_SAVING_COLLECTION`은 web 소유의 임시 저장소지만 운영 이름도 같은 v4 namespace로 통일한다.

`web_app.langflow_client`는 질의 화면에서 `LANGFLOW_ROUTER_API_URL` 또는 `LANGFLOW_BASE_URL + LANGFLOW_ROUTER_FLOW_ID`로 만든 `/api/v1/run/{flow_id}` URL만 호출한다. Router Flow가 내부 API caller로 선택된 하위 Flow를 실행하므로 web backend는 `selected_flow`별 URL을 추가 호출하지 않는다. Metadata saving 전용 화면은 별도 saving Flow URL을 계속 사용할 수 있다.

개별 subflow가 구조화 `api_response` Data output 대신 Chat/Message Output만 반환하는 경우도 지원한다. 이때 web app은 nested message text를 `answer_message`로 표시하고 `message_only=true`로 다룬다. 다만 결과 row, state, data_ref, intent, pandas code 같은 구조화 영역은 Message 안에 JSON으로 포함되어 있거나 별도 Data/API response output으로 반환될 때만 화면의 표/상세 탭에 안정적으로 표시된다.

`MONGODB_RESULT_COLLECTION`은 metadata가 아니라 query result row 저장소다. `23 MongoDB Result Store`는 pandas 직후 source rows와 result rows를 저장한다. 다음 turn에서는 compact state를 그대로 넘기는 것이 기본이며, optional first `05 MongoDB Previous Result Loader`는 이전 state에 `data_ref`만 있고 preview/summary가 없을 때 사용한다. 이전 결과 전체 rows가 필요한 후속 분석은 data analysis flow의 “이전 결과 복원” 브랜치에서 MongoDB loader를 실행한다.

## 7. 화면 구성

### 7.1 질의/분석 화면

필수 UI:

- 질문 입력창
- session 선택 또는 새 대화 시작
- 실행 버튼
- 최종 답변 영역
- 결과 table 영역
- 사용 dataset/source alias 영역
- 적용 filter/param 영역
- 의도 분석 요약 영역
- pandas 처리 요약과 code 접기 영역
- 오류/경고 영역
- 전체 row 다운로드 또는 상세 보기 버튼

표시 필드:

- `answer_message`
- `data.columns`
- `data.rows`
- `data.row_count`
- `data.data_ref`
- `applied_scope.datasets`
- `applied_scope.source_aliases`
- `applied_scope.filters_by_source`
- `applied_scope.params_by_source`
- `intent_plan.intent_type`
- `intent_plan.analysis_kind`
- `intent_plan.step_plan`
- `intent_plan.retrieval_jobs`
- `analysis.status`
- `analysis.safety_passed`
- `analysis.executed`
- `analysis.analysis_code` 또는 `analysis.pandas_code_json.code`
- `analysis.errors`

기본 정책:

- pandas code는 기본 접힘 상태로 둔다.
- `data_ref`는 사용자가 직접 수정할 값이 아니라 전체 결과 로딩/다운로드용 참조로만 쓴다.
- 후속 질문을 위해 `state`는 backend session store에 저장한다.

### 7.2 Metadata 등록 허브

탭은 세 개로 나눈다.

- Domain 용어/규칙 등록
- Table catalog 데이터셋 등록
- Main flow filter/parameter 등록

공통 필수 UI:

- 자연어 설명 입력창
- 예시 문장 선택 버튼
- 저장 방식 선택
- 실행 버튼
- 정제된 설명 표시
- 생성 후보 item 표시
- 부족한 정보 표시
- 비슷한 기존 정보 표시
- 충돌/경고 표시
- 저장 결과 표시
- 고급 JSON 보기 접기 영역

저장 방식 문구:

| 내부 action | 화면 문구 | 의미 |
| --- | --- | --- |
| `merge` | 기존 내용 보강 | 기존 doc과 새 payload를 deep merge |
| `replace` | 기존 내용 교체 | 같은 key의 기존 doc을 새 item으로 교체 |
| `skip` | 기존 항목 유지 | 같은 기존 doc은 변경하지 않고 중복 쓰기만 건너뜀 |
| `create_new` | 새 key로 등록 | 기존 key와 다를 때만 신규 저장 |

기본값은 `skip`으로 둔다. 실제 승인 대기·재개가 필요하면 별도 pending 저장소와 승인 API를 사용한다.

### 7.3 중복/유사 정보 처리 화면

필수 UI:

- 새 입력으로 생성된 item
- 비슷한 기존 item
- `existing_matches.reason` 또는 `conflict_warnings.reason`
- 추천 action이 있으면 표시
- 선택 버튼: 보강, 교체, 저장 안 함, 새 key로 저장
- 새 key 입력창
- 선택 action으로 재실행 버튼

Backend 처리:

1. 기본 실행은 `duplicate_action=skip`으로 기존 중복 문서를 보존한다.
2. `existing_matches`와 `conflict_warnings`를 화면에 표시한다.
3. 사용자가 명시적으로 보강·교체·새 key 저장을 선택한 경우 같은 `raw_text`와 선택 action으로 다시 실행한다.
4. 승인 대기 상태가 필요하면 Flow 내부가 아니라 별도 pending 저장소/API로 구현한다.

### 7.4 Metadata 조회/검토 화면

필수 UI:

- metadata type 필터: `domain`, `table_catalog`, `main_flow_filter`
- key 검색
- alias 검색
- dataset family/source type 검색
- active/inactive 상태 필터
- 상세 보기
- JSON 보기
- 자연어로 수정 요청 버튼

일반 사용자는 직접 JSON 수정 버튼 대신 자연어 수정 요청을 사용한다. Backend는 기존 metadata 요약을 자연어 등록 화면의 context로 넘기고, 실제 저장은 saving flow를 통해 수행한다.

### 7.5 등록 후 검증 화면

필수 UI:

- smoke question 목록
- 직접 질문 입력
- 실행 버튼
- 성공/실패 표시
- 기대 dataset/filter 표시
- 실제 `applied_scope.datasets`, `filters_by_source`, `params_by_source`
- 결과 row count
- 오류 메시지

검증 질문 범주:

- 방금 추가한 domain 용어가 intent에 반영되는 질문
- 방금 추가한 dataset이 선택되는 질문
- 방금 추가한 filter가 retrieval/pandas 조건으로 적용되는 질문
- 중복 metadata가 agent 판단을 흐리지 않는지 확인하는 질문
- 후속 질문에서 이전 `state.current_data`를 재사용하는 질문

## 8. Backend API 설계

권장 endpoint:

| Method | Path | 역할 |
| --- | --- | --- |
| `GET` | `/api/health` | Web, Langflow, MongoDB, result store 연결 상태 확인 |
| `POST` | `/api/query` | main query/analysis flow 실행 |
| `GET` | `/api/query/{query_id}/rows` | `data_ref` 기반 전체 row 조회 또는 다운로드 |
| `GET` | `/api/sessions/{session_id}` | session state 조회 |
| `DELETE` | `/api/sessions/{session_id}` | session 초기화 |
| `POST` | `/api/metadata/domain/run` | domain saving flow 실행 |
| `POST` | `/api/metadata/table-catalog/run` | table catalog saving flow 실행 |
| `POST` | `/api/metadata/main-filter/run` | main flow filter saving flow 실행 |
| `GET` | `/api/metadata/{type}` | metadata 검색 |
| `GET` | `/api/metadata/{type}/{key}` | metadata 상세 조회 |
| `POST` | `/api/metadata/{type}/validate` | 저장 후 main flow smoke 검증 |
| `GET` | `/api/metadata/pending/{pending_id}` | 중복 선택 pending record 조회 |
| `DELETE` | `/api/metadata/pending/{pending_id}` | pending record 취소 |

### 8.1 `/api/query` request

```json
{
  "session_id": "user-session-id",
  "question": "오늘 DA, WB공정에서 각각 재공 상위 3개 제품을 뽑아줘"
}
```

Backend 처리:

1. `session_id`로 이전 `state` 조회.
2. Langflow router flow를 호출한다. Web backend는 selected subflow를 추가로 직접 호출하지 않는다.
3. API Router가 선택해 실행한 하위 Flow의 `api_response` Data output 또는 최종 Message output을 읽는다.
4. 응답의 `state`를 session store에 저장.
5. 화면용 response 반환.

### 8.2 `/api/metadata/{type}/run` request

```json
{
  "raw_text": "wip_today는 Oracle PNT_RPT에서 SELECT WORK_DT, OPER_NAME, WIP FROM PKG_WIP_TODAY WHERE WORK_DT = {DATE}로 조회해. DATE는 WORK_DT에 매핑해.",
  "duplicate_action": "skip"
}
```

Backend 처리:

1. `type`에 맞는 Langflow saving flow id 선택.
2. full collection name과 MongoDB 설정을 flow input 또는 노드 입력값 설정으로 주입.
3. `10 ... API Response Builder.api_response`를 읽는다.
4. 중복 skip/merge/replace/create_new 결과를 `operation_by_key`로 반환한다.
5. 저장 성공이면 등록 후 검증 화면에서 실행할 smoke question 후보를 생성한다.

### 8.3 Saving response normalization

Backend는 Langflow `api_response`를 거의 그대로 전달하되, 화면 상태 계산 필드를 추가해도 된다.

```json
{
  "ui_status": "saved | needs_more_input | duplicate_choice_required | warning | error",
  "status": "skipped",
  "message": "...",
  "metadata_type": "table_catalog",
  "items": [],
  "existing_matches": [],
  "conflict_warnings": [],
  "review": {},
  "write_result": {},
  "trace": {},
  "pending_saving_id": "optional"
}
```

`ui_status`는 backend 편의 필드다. Langflow flow 자체의 기본 status 값은 `ok`, `skipped`, `error`를 유지한다.

## 9. Langflow Run API 호출 시 입력 매핑

Langflow Run API wrapper는 flow별 parser와 노드 입력값 설정 builder를 분리한다.

Query runtime에서 기본 전달 값:

- `input_value`: 사용자 질문 text
- `session_id`: 웹/API 대화별 session id
- router flow 노드 입력값 설정: Smart Router 최종 구조에서는 제거된 `00 라우터 요청 로더` tweak을 전달하지 않는다
- route별 API caller 설정: bundle이 입력한 하위 Flow의 고정 `endpoint_name` 경로를 사용하며 import 후 Flow ID를 수동 입력하지 않는다

Router의 `Chat Input.message`는 Smart Router에만 연결합니다. Langflow가 각 API caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 `session_source` fan-out edge는 만들지 않습니다. 이 fan-out은 Smart Router를 다시 빌드해 질문 Message를 반복 저장할 수 있습니다.

MongoDB URI/database/collection override는 운영에서는 env를 우선 사용한다. 특정 Langflow deployment에서 env를 쓸 수 없을 때만 metadata loader, result store, session state loader/writer의 advanced Mongo 입력을 노드 입력값 설정으로 넣는다.

Saving flow에 전달할 값:

- `00 ... Saving Request Loader.raw_text`
- `00 ... Saving Request Loader.duplicate_action`
- `00 ... Existing Items Loader.mongo_uri/mongo_database/collection_name`
- `05 ... Similarity Checker.mongo_uri/mongo_database/collection_name`
- `07 ... Review Writer.mongo_uri`
- `07 ... Review Writer.mongo_database/collection_name`

Parsing 원칙:

- Query flow: 선택된 하위 Flow의 `api_response` Data output을 우선 사용한다. 이 payload는 답변, preview data, `data_ref`, state만 projection한 상태다.
- Saving flow: `10 ... API Response Builder.api_response`를 우선 사용한다.
- `21 Answer Message Adapter.message`와 saving flow의 `message`는 사람이 보는 Markdown/text다. JSON 계약으로 파싱하지 않는다.
- Langflow wrapper가 response를 여러 겹 감싸더라도 backend parser는 대상 output name을 찾아 꺼낸다.

## 10. Pending Saving 저장

중복 선택이 필요한 경우 backend가 pending record를 저장한다.

```json
{
  "_id": "pending_saving_id",
  "metadata_type": "domain",
  "raw_text": "...",
  "last_response": {},
  "created_by": "user id",
  "created_at": "ISO datetime",
  "expires_at": "ISO datetime"
}
```

보관 기간은 1~7일 정도가 적당하다.

Pending record에 저장하지 않을 값:

- MongoDB URI
- Langflow API key
- LLM API key
- Oracle/H-API/Datalake/Goodocs credential
- full source rows

## 11. 권한

권장 역할:

| Role | 가능 작업 |
| --- | --- |
| Viewer | 질의/분석 실행, metadata 조회 |
| Editor | 자연어 metadata 등록 요청, 등록 후 검증 실행 |
| Approver | 중복 처리 선택, 저장 승인 |
| Admin | flow id, collection name, 권한, 배포 설정 관리 |

운영 초기에는 `Editor`와 `Approver`를 분리하는 것이 안전하다.

## 12. 구현 순서

1. Backend health check와 Langflow Run API wrapper 구현
2. router/subflow output에서 compacted payload를 꺼내는 parser 구현
3. `/api/query`와 session state store 구현
4. result `data_ref` 기반 전체 row 조회 endpoint 구현
5. 질의/분석 화면 구현
6. saving flow별 `api_response` parser 구현
7. `/api/metadata/{type}/run` 공통 handler 구현
8. Metadata 등록 허브 화면 구현
9. 중복/유사 정보 선택 화면과 pending store 구현
10. Metadata 조회/검색 화면 구현
11. 등록 후 검증 화면 구현
12. 권한, 감사 로그, 운영 배포 설정 추가

## 13. 완료 기준

### 질의/분석 화면

- 사용자가 질문을 입력하면 답변과 table preview를 한 화면에서 확인할 수 있다.
- `row_count`와 preview row가 구분된다.
- 전체 결과가 필요하면 `data_ref`로 전체 row를 불러올 수 있다.
- 사용 dataset, source alias, 적용 filter/param을 볼 수 있다.
- 후속 질문에서 이전 결과를 참조할 수 있다.

### Metadata 등록 화면

- 사용자가 JSON 없이 자연어로 입력할 수 있다.
- 부족한 정보가 있으면 저장되지 않고 한국어로 안내된다.
- 비슷한 기존 정보가 있으면 저장되지 않고 선택지가 표시된다.
- 저장 성공 시 저장된 key와 count가 표시된다.
- `api_response.trace.refined_text`는 고급 접기 영역에서만 표시한다.

### Metadata 검증 화면

- 등록 직후 smoke 질문을 실행할 수 있다.
- 기대 dataset/filter와 실제 `applied_scope`를 비교할 수 있다.
- 실패 시 intent, retrieval, pandas, answer 중 어느 단계에서 실패했는지 볼 수 있다.

## 14. 구현 시 주의사항

- 일반 metadata 저장은 saving flow를 통해서만 수행한다.
- Web backend의 MongoDB 직접 write는 관리자용 예외 또는 pending/session/result store에 한정한다.
- metadata collection과 result row collection을 섞지 않는다.
- table catalog metadata에는 credential을 저장하지 않는다. `db_key`, `doc_id`, endpoint id 같은 참조만 저장한다.
- source credential은 backend secret store 또는 배포 환경변수로 관리한다.
- `04A.retrieval_mode=dummy` 상태에서는 deterministic dummy rows를 사용한다. `07`은 payload의 동일 모드를 읽으므로 별도 조회 모드 설정이 없다.
- Browser에는 Langflow response payload 전체를 그대로 노출하지 않는다.
- 오류 메시지는 사용자 메시지와 기술 로그를 분리한다.
- `state`, `current_data`, `followup_source_results`, `data_ref`는 session store에 유지한다.
- full prompt, raw source rows, MongoDB URI는 화면 response와 pending record에서 제외한다.

## 15. Web 검증 체크리스트

- Router Flow의 Smart Router output이 route별 API caller 하나만 실행하는가
- API caller의 `message`와 `status_data`가 모두 생성되면서 실제 HTTP 호출은 한 번만 수행되는가
- 별도 `session_source` edge 없이 API caller의 자동 주입 `session_id`로 2-turn session이 유지되는가
- 선택된 하위 Flow의 compact `api_response`를 받을 수 있는가
- 선택된 flow의 API/Data output을 우선 파싱하는가
- `21 Answer Message Adapter.message`를 API JSON으로 오인하지 않는가
- session별 follow-up 질문이 유지되는가
- `state.current_data.data_ref`가 다음 질문 전에 preview/summary로 복원되고, full rows가 초반 payload에 불필요하게 붙지 않는가
- `MONGODB_RESULT_COLLECTION`에 큰 row가 저장되고 전체 row 조회가 되는가
- domain saving에서 `count_distinct`가 `nunique`로 정규화되는가
- table catalog saving에서 source 필수 정보 누락 시 저장이 차단되는가
- main flow filter saving에서 alias/column 중복 경고가 표시되는가
- `duplicate_action=skip`일 때 기존 문서를 변경하지 않고 skipped 결과를 표시하는가
- `duplicate_action=merge/replace` 선택 후 저장 결과가 표시되는가
- 저장 후 main flow metadata loader가 새 metadata를 읽는가
- saving flow의 `10 ... API Response Builder.api_response`를 화면 계약으로 사용하고 있는가
- Browser에 secret이 노출되지 않는가





