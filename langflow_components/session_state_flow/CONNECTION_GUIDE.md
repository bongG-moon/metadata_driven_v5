# Session State Flow Connection Guide

이 flow는 Langflow Playground나 API 실행에서 다음 질문이 이전 답변의 조건과 결과를 이어받을 수 있도록 compact state를 MongoDB에 저장하고 다시 불러오는 공통 부품이다.

## 기본 연결

```text
Chat Input.message
  -> 00 MongoDB 세션 상태 로더.question
  -> 00 분석 요청 로더.question

00 MongoDB 세션 상태 로더.loaded_state
  -> 00 분석 요청 로더.previous_state

20 답변 응답 생성기.payload_out
  -> 01 MongoDB 세션 상태 저장기.response_payload

01 MongoDB 세션 상태 저장기.payload_out
  -> 21 답변 메시지 어댑터.payload
  -> 22 API 응답 생성기.payload
```

`Chat Input.message`는 로더와 실제 request loader에 동시에 연결한다. 로더는 message 객체의 `session_id`, `conversation_id`, `chat_id`, `thread_id`를 우선 사용하고, 없으면 저장된 state나 `demo-session`을 사용한다.

## MongoDB 기본값

| 항목 | 기본값 |
| --- | --- |
| Database | `datagov` |
| Collection | `agent_v4_session_states` |
| Env URI | `MONGODB_URI` |
| Env Collection | `MONGODB_SESSION_STATE_COLLECTION` |

저장 문서 `_id`는 `session_state:<session_id>` 형식이다.

## 저장되는 내용

저장기는 full rows를 session state에 반복 저장하지 않는다. 다음 턴 판단에 필요한 아래 compact 정보만 저장한다.

| 필드 | 용도 |
| --- | --- |
| `last_question` | 이전 질문 조건 상속 판단 |
| `last_answer_message` | 이전 답변 참조 질문 해석 |
| `last_intent_plan` | 이전 metric, retrieval job, pandas 계획 상속 판단 |
| `last_applied_criteria` | 이전 필수 파라미터, 분석 필터, group/metric 확인 |
| `current_data` | 결과 컬럼, preview rows, row_count, data_ref |
| `followup_source_results` | 이전 source별 컬럼, data_ref, 적용 조건 |
| `runtime_source_refs` | source alias별 원본 data_ref |

원본 전체 rows는 `23 MongoDB 결과 저장기`의 result store에 `data_ref`로 저장하고, session state에는 참조만 남기는 것을 원칙으로 한다.

## Data Analysis Flow 연결 위치

- 시작부: `00 MongoDB 세션 상태 로더.loaded_state`를 `00 분석 요청 로더.previous_state`에 연결한다.
- 종료부: `20 답변 응답 생성기.payload_out`을 `01 MongoDB 세션 상태 저장기.response_payload`에 연결하고, 저장기 출력을 `21`, `22`로 넘긴다.

이렇게 연결하면 “오늘 WB공정 생산량 알려줘” 다음에 “어제 생산량은?”처럼 질문해도 이전 state가 00번에 자동 주입된다.
