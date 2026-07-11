# Metadata QA Flow 연결 가이드

권장 import 파일은 `flow_exports/metadata_qa_flow_v5_standalone.json`이다.

## 연결 순서

```text
Chat Input.message -> 00 Request Loader.question
00.payload_out -> 02 Context Builder.payload
01A.domain_items -> 02.domain_items
01B.table_catalog_items -> 02.table_catalog_items
01C.main_flow_filters -> 02.main_flow_filters
02.payload_out -> 03 Variables.payload
03 outputs -> Prompt Template variables
Prompt -> Agent -> 04 Normalizer.llm_response
02.payload_out -> 04 Normalizer.payload
04.payload_out -> 05 Message Adapter.payload
04.payload_out -> 06 API Response.payload
05.message -> 06.display_message / Chat Output
```

## Payload 정책

- MongoDB loader projection은 identity, `status`, `payload`만 읽고 등록 trace와 writer 상태는 조회하지 않는다.
- `02`는 질문 모드를 먼저 결정한 뒤 필요한 필드만 LLM context에 포함한다.
- `available_sources`: compact candidate rows만 전달하고 `query_template`은 제외한다.
- `dataset_sql`: 선택된 dataset의 SQL만 포함한다.
- 기본 최대 후보는 50, 기본 context 제한은 65,536 bytes다.
- `max_items`와 `max_bytes`는 실제 상한으로 동작하며 축소 시 trace warning을 남긴다.
- secret 값은 `***`로 마스킹한다.

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
