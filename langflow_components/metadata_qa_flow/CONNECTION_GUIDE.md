# Metadata QA Flow 연결 가이드

권장 import 파일은 `flow_exports/metadata_qa_flow_v5_standalone.json`이다.

## 연결 순서

```text
Chat Input.message -> Session State Loader.question / 00 Request Loader.question
Session State Loader.loaded_state -> 00 Request Loader.previous_state
00.payload_out -> 01 Snapshot Loader.request_payload / 02 Context Builder.payload
01.domain_items -> 02 Context Builder.domain_items
01.table_catalog_items -> 02 Context Builder.table_catalog_items
01.main_flow_filters -> 02 Context Builder.main_flow_filters
02.payload_out -> 03 Variables.payload
03 outputs -> Prompt Template variables
Prompt -> 기본 Language Model.input_value
기본 Language Model.text_output -> 04 Normalizer.llm_response
02.payload_out -> 04 Normalizer.payload
04.payload_out -> Session State Writer.response_payload
Session State Writer.payload_out -> 05 Message Adapter.payload / 06 API Response.payload
05.message -> 06.display_message / Chat Output
```

## Payload 정책

- MongoDB loader projection은 identity, `status`, `payload`만 읽고 등록 trace와 writer 상태는 조회하지 않는다.
- 통합 Snapshot Loader는 한 MongoClient로 세 컬렉션을 순차 조회하고 빈 질문이면 접속 전에 `skipped`로 종료한다.
- 정상 전체 snapshot은 프로세스 안에서 기본 15초간 캐시한다. partial/error 결과는 캐시하지 않으며 `METADATA_QA_CACHE_TTL_SECONDS=0`이면 비활성화된다.
- 실제 metadata 저장 성공 시 같은 worker의 snapshot generation을 증가시켜 즉시 무효화한다. 다른 worker의 오래된 snapshot은 TTL 안에서만 유지될 수 있다.
- `02`는 질문 모드를 먼저 결정한 뒤 필요한 필드만 LLM context에 포함한다.
- `available_sources`: compact candidate rows만 전달하고 `query_template`은 제외한다.
- `scoped_sources`: 질문에 명시된 Table Catalog 분류·연결방식·DB/소스와, 같은 세션의 직전 목록 key를 모두 만족하는 데이터셋만 표시한다.
- `dataset_comparison`: 질문에 직접 언급된 둘 이상의 데이터셋만 `용도·사용 시점`, `기준 구분`, `연결 방식`, `필수 조건`으로 비교한다. 등록되지 않은 시간 기준은 추정하지 않는다.
- `여기서`·`그중` 데이터셋 후속질문은 세션의 작은 `metadata_qa_inventory` allowlist를 사용한다. 이전 목록이 없거나 현재 카탈로그와 맞지 않으면 전체 목록으로 넓히지 않고 다시 목록을 요청한다.
- `dataset_sql`: 선택된 dataset의 SQL만 포함한다.
- 기본 최대 후보는 50, 기본 context 제한은 65,536 bytes다.
- `max_items`와 `max_bytes`는 실제 상한으로 동작하며 축소 시 trace warning을 남긴다.
- secret 값은 `***`로 마스킹한다.
- Tool이 필요 없는 QA 생성은 Langflow 기본 `Language Model`을 사용하므로 외부 모델 요청에 빈 `tools` 배열을 전달하지 않는다.
- 결정론적 답변 모드는 `answer_policy.mode=deterministic_context`로 표시하고 모델 응답이 있더라도 표·답변은 authoritative context를 우선한다.
- 자유 서술 모드는 `answer_policy.mode=model_assisted`이며 기본 Language Model 응답을 정규화해 사용한다.
- 단순한 단일 출력 구조를 유지하기 위해 기본 Language Model은 모든 유효 질문에서 실행된다. 결정론 모드의 응답 사용 여부는 04 Normalizer trace에서 확인한다.
- Session State Loader/Writer의 `Mongo URI 선택값`은 다른 멀티턴 Flow와 동일하게 Langflow 전역 변수 `MONGO_URL`을 사용한다. Mongo 연결이 없으면 목록 후속질문만 unavailable이고, 단일 턴 QA는 계속 처리한다.

## 응답 계약

- `response_type=metadata_qa`
- `direct_response_ready=true`
- 표시용 canonical 필드는 `message`
- 표 행은 `data.rows` 한 곳에 두고 `answer_sections.detail_table.row_source=data.rows`로 참조한다.
- `answer_sections`는 요약, 표 metadata, SQL block, route hint 등 UI 구조를 유지한다.

## 06 API Router 연결

```text
Chat Input.message
  -> Smart Router.input_text

Smart Router.metadata_qa
  -> 01 선택 Flow API 메시지 호출기.flow_input

01.message
  -> Chat Output
```

API route의 Smart Router `Route Message`는 비워 원래 사용자 질문이 전달되게 한다. 별도 `session_source` edge는 두지 않고 API 호출기가 부모 실행 세션을 자동 상속한다. 07 Agent + Tool Router는 별도로 제공되는 비교용 대안이다.
