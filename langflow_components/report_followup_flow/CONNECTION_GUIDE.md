# 10. v5_report_followup 연결 가이드

이 Flow는 같은 세션에서 생성된 Report가 저장한 materialized query source만 다시 불러와 후속 질문에 답합니다. Table Catalog, Main Filter, 신규 데이터 조회, join, 자유 pandas 코드는 사용하지 않습니다.

## 노드 연결

```text
Chat Input.message
  -> 공용 MongoDB Session State Loader.question
  -> 00 Report 후속 질문 Prompt 생성기.question

Session State Loader.loaded_state
  -> 00 Report 후속 질문 Prompt 생성기.loaded_state

00.payload_out -> 00B Guarded Plan Router.payload
00.prompt -> 00B Guarded Plan Router.prompt
00.payload_out -> 01 Report 후속 실행 계획 검증기.payload
00B.text_output -> 01.llm_response
01.payload_out -> 공용 MongoDB Result Loader.payload
Result Loader.payload_out -> 02 Report Snapshot 결정론적 실행기.payload
02.payload_out -> 03 Report 후속 응답 생성기.payload
03.payload_out -> 공용 MongoDB Session State Writer.response_payload
Session State Writer.payload_out -> 04 Report 후속 API 종료 어댑터.response_payload
04.message -> Chat Output.input_value
```

Router의 이름 기반 Run Flow Tool은 `04.message -> Chat Output`의 단일 native Message를 최종 답변으로 사용합니다. `04.api_response`는 Flow를 API로 직접 호출할 때 사용할 수 있는 별도 leaf 출력이며 Router 응답 경로에는 연결하지 않습니다.

## 실행 경계

- Report가 `report.query_source.v1`로 선언하고 result store에 함께 저장한 source만 실행합니다.
- context 누락·만료·확인 필요·신규 조회 위임 상태는 00B가 LLM을 호출하지 않고 결정론적으로 전달합니다.
- 실행 직전 복원된 `source_results[].query_source_contract`가 `authoritative=true`인지 다시 확인합니다.
- 지원 연산은 `filter`, `sort(nulls last)`, `top_n`, `select`입니다.
- `현재 기준`, `현재 데이터`, `지금 시점`, `최신 데이터`, `다시 조회`, `새로 조회`와 외부 source 결합 요청은 `live_retrieval_required`로 차단합니다.
- `현재작업재공`처럼 `현재`가 물리 컬럼명 일부인 경우에는 최신 데이터 요청으로 해석하지 않습니다.
- Report의 실제 물리 컬럼명을 사용하므로 `DEN`/`DENSITY` 같은 전역 alias 매핑이 필요하지 않습니다.

## 상태 보존

성공 응답은 원래 `current_data.report_context`, `data_ref`, `runtime_source_refs`를 그대로 유지하고 `current_data.current_view_plan`과 `last_intent_plan.report_execution_plan`만 갱신합니다. 따라서 다음 `그중` 질문에서도 같은 Report anchor를 재사용할 수 있습니다.
