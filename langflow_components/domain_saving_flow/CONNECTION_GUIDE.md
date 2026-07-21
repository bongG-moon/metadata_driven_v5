# Domain Saving Flow 연결 가이드

권장 import 파일은 `flow_exports/domain_saving_flow_v5_standalone.json`이다. 기존 MongoDB 문서 형식과 `agent_v4_domain_items` 컬렉션은 유지한다.
추출 prompt는 외부 Markdown을 Langflow Prompt Template 노드에 넣고 Langflow Agent/LLM에 연결한다.

## 최적화된 경로

```text
Chat Input -> 00 Request Loader -> 03 Variables -> Saving Prompt -> Extraction Agent
Request + Agent -> 04 Normalizer -> 05 동일 Key 조회기
05 -> 07 단일 Writer -> 08 단일 Response -> 09 Message -> Chat Output 1개
                                      \-> 10 API
```

- `05`는 후보가 확정된 뒤 해당 `section/key`와 같은 section의 key/alias/display_name만 MongoDB에서 제한 조회한다. 선행 전체 문서 loader는 사용하지 않는다.
- Domain identity는 같은 section 안에서만 비교하며, exact key를 우선하고 `BG`, `B/G`, `B-G`, `B_G` 같은 영문·숫자 식별자는 separator를 제거한 exact token으로 비교한다. 부분 문자열이나 공정 목록 유사도는 identity로 사용하지 않는다.
- `05`는 중복 판정용 읽기, `07`은 live 저장과 쓰기 직전 재확인 역할이므로 두 노드의 MongoDB 입력은 유지한다.
- Dry Run과 Live 모두 metadata 추출 LLM 1회만 호출하며, Writer가 스키마·credential을 결정론적으로 검증한다.
- 그래프 분기가 없으므로 Playground용 Chat Output은 하나다.

## 중복 처리

- `skip`(기본값): 기존 문서를 유지하고 해당 문서 쓰기 생략
- `merge`: 기존 payload에 새 필드를 재귀 병합
- `replace`: 같은 key 또는 유일한 강한 identity match가 있으면 기존 canonical `_id`를 새 후보로 교체하고, match가 없으면 신규 저장
- `create_new`: 후보 key가 비어 있으면 그대로 신규 저장하고, 동일 `_id`가 이미 있을 때만 `<key>_copy`, `<key>_copy_2` 순서로 새 key 생성

identity match가 여러 기존 문서와 겹치거나 기존 identity 조회 자체가 실패하면 `replace`, `merge`, `skip`은 저장하지 않고 오류를 반환한다. `operation_by_key`에는 요청 key, 실제 target key, target `_id`, `inserted/replaced/merged/skipped/created_new`가 기록되며 채팅 표에도 처리 구분이 표시된다.

## 안전 규칙

- domain에는 `source_config`와 query를 저장하지 않는다.
- secret/credential 계열 key는 결정론 검사에서 차단한다.
- `registration_trace.raw_text` 위치는 유지하되 secret을 `***`로 치환하고 2,000자로 제한한다.
- `dry_run`은 Bool toggle이다.
- `revision`, `request_id`, idempotency 필드는 추가하지 않는다.
- standalone Langflow에는 실행을 저장한 채 사용자 결정을 기다렸다 재개하는 HITL 상태가 없으므로 `ask` 모드는 제공하지 않는다.

Playground는 `09.message`, Web/API는 `10.api_response`를 사용한다. API 응답의 canonical 표시 필드는 `message`이며 표 행은 `data.rows`에만 둔다.
