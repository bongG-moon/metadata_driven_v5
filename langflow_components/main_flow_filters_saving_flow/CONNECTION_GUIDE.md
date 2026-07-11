# Main Flow Filter Saving Flow 연결 가이드

권장 import 파일은 `flow_exports/main_flow_filter_saving_flow_v5_standalone.json`이다. 기존 MongoDB 문서 형식과 `agent_v4_main_flow_filters` 컬렉션은 유지한다.
추출 prompt는 외부 Markdown을 Langflow Prompt Template 노드에 넣고 Langflow Agent/LLM에 연결한다.

## 최적화된 경로

```text
Chat Input -> 00 Request Loader -> 03 Variables -> Saving Prompt -> Extraction Agent
Request + Agent -> 04 Normalizer -> 05 동일 Key 조회기
00 Existing Loader ---------------------> 05 동일 Key 조회기
05 -> 07 단일 Writer -> 08 단일 Response -> 09 Message -> Chat Output 1개
                                      \-> 10 API
```

- Existing Loader는 기존 전체 문서에서 `registration_trace`만 제외해 Matcher에 직접 전달한다. Request/LLM payload에는 싣지 않는다.
- 로더 제한 밖 후보는 `05`가 해당 `filter_key`를 MongoDB에서 정확 조회하므로 중복을 놓치지 않는다.
- Dry Run과 Live 모두 metadata 추출 LLM 1회만 호출하며, Writer가 스키마·credential을 결정론적으로 검증한다.
- 그래프 분기가 없으므로 Playground용 Chat Output은 하나다.

## 중복 처리

- `skip`(기본값): 기존 문서를 유지하고 해당 문서 쓰기 생략
- `merge`: 기존 payload에 새 필드 병합
- `replace`: 같은 `_id` 문서 교체
- `create_new`: `<filter_key>_copy`, `<filter_key>_copy_2` 순서로 새 key 생성

## 안전 규칙

- main filter에는 `source_config`와 query를 저장하지 않는다.
- secret/credential 계열 key는 저장 전에 차단한다.
- `registration_trace.raw_text` 위치는 유지하되 secret 제거와 2,000자 제한을 적용한다.
- `dry_run`은 Bool toggle이다.
- `revision`, `request_id`, idempotency 필드는 추가하지 않는다.
- standalone Langflow에는 실행을 저장한 채 사용자 결정을 기다렸다 재개하는 HITL 상태가 없으므로 `ask` 모드는 제공하지 않는다.

Playground는 `09.message`, Web/API는 `10.api_response`를 사용한다. API 응답의 canonical 표시 필드는 `message`이며 표 행은 `data.rows`에만 둔다.
