# Route Flow API 호출 방식 설계 (06)

`route_flow`는 Smart Router로 route를 고른 뒤, 선택된 branch에서 하위 Langflow flow를 Run API로 호출하는 구조입니다.
06은 Run Flow/Tool Call 방식이 아니라 API 호출 방식으로 고정합니다.

## 1. 설계 목표

1. 사용자가 입력한 원문이 하위 flow의 `input_value`로 그대로 들어간다.
2. route branch마다 화면에서 직접 관리할 값은 `하위 Flow API URL`과 필요한 경우 `Langflow API 키`뿐이다.
3. 06 API Router는 하위 flow 응답을 복제하지 않고 표시 Message와 최소 구조화 상태 Data만 반환한다.
4. API 응답 형식이 필요하면 각 하위 flow 안에서 해당 Message 형식을 만들도록 한다.
5. route명/flow명 alias mapping 같은 중복 설정을 router 컴포넌트 안에 두지 않는다.
6. 하위 flow 호출 시 `session_id`를 함께 보내 기본 세션 공유로 인한 엉뚱한 답변을 방지한다.

## 2. 최종 구조

```text
Chat Input.message
  -> Smart Router.input

Smart Router.<route output>
  -> 01 선택 Flow API 메시지 호출기.flow_input

01 선택 Flow API 메시지 호출기.message
  -> Chat Output.input
```

Chat Input은 Smart Router에만 연결합니다. API caller의 `session_id` 입력은 Langflow graph가 부모 실행 session으로 자동 주입합니다. Chat Input을 다섯 caller에 추가 연결하면 graph가 다시 빌드되며 Smart Router의 비선택 branch 중지 상태가 풀릴 수 있으므로 금지합니다.

`direct_answer`, `clarification`처럼 하위 flow가 필요 없는 route는 Smart Router Route Message를 각 Chat Output에 바로 연결합니다. Smart Router가 선택되지 않은 dynamic output을 중단하므로 중간 terminal Gate는 필요하지 않습니다. Gate를 별도 terminal로 두면 Playground가 이를 평가하며 질문 JSON 카드를 추가로 표시할 수 있습니다.

## 3. Smart Router Route Message 정책

API 호출 route의 Route Message는 비웁니다.
Langflow Smart Router는 Route Message가 있으면 원문 대신 Route Message를 output으로 내보낼 수 있기 때문입니다.

| route 유형 | Route Message |
| --- | --- |
| data analysis / metadata QA / saving flow 호출 | 비움 |
| direct answer / clarification | 사용자에게 보여줄 문장 |

## 4. API 호출 컴포넌트

`01 선택 Flow API 메시지 호출기`는 표시와 운영 상태를 분리합니다.

| 구분 | 값 |
| --- | --- |
| 입력 | `Flow 입력`, `하위 Flow API URL`, secret API key, `세션 ID`, `세션 원본 Message`, route 이름, connect/read timeout |
| 출력 | `메시지`, `호출 상태 Data` |
| API payload | `input_value`, `input_type=chat`, `output_type=chat`, `session_id` |

`세션 ID`는 advanced 입력입니다.
비워두면 컴포넌트가 격리용 session_id를 자동 생성합니다.
웹처럼 같은 대화 세션을 이어야 하는 환경에서는 외부 session_id를 넣어 하위 flow도 같은 대화 맥락을 사용하게 합니다.

## 5. 응답 처리

Langflow API 응답은 버전과 flow 구성에 따라 nested 구조가 조금씩 다를 수 있습니다.
컴포넌트는 아래 후보를 재귀적으로 찾아 첫 번째 Message 텍스트를 반환합니다.

- `display_message`
- `answer_message`
- `message`
- `text`
- `content`
- `outputs/results/message/text`
- JSON 문자열 안의 `api_response.message`

Playground 최종 표시는 Message를 사용합니다. 운영 로그와 API 상태 확인에는 Data output을 사용합니다. 두 output이 함께 연결되어도 컴포넌트 내부 캐시로 하위 API는 한 번만 호출합니다.

## 6. 검증 기준

- route output에 원문이 들어오면 API payload의 `input_value`가 원문과 같고 `session_id`가 함께 전달된다.
- Chat Input outgoing edge는 Smart Router 한 개뿐이며 API caller `session_source` edge는 0개다.
- route output에 `{"route":"..."}`만 들어오면 API 호출을 막고 Route Message를 비우라는 안내를 반환한다.
- 하위 flow가 Chat Output 메시지를 반환하면 `01.message`에 그 텍스트가 나온다.
- Router custom component는 API 호출기 하나만 사용하고 direct terminal용 중간 Gate는 존재하지 않는다.
- 테스트와 문서에는 예전 3단계 노드나 별도 구조화 envelope 안내가 남아 있지 않다.
- 새 06 import 후 운영 provider 실행에서 질문 Machine echo 0건, SmartRouter build 1회, 선택된 하위 API 호출 1회를 확인한다.
- 같은 session 전체 메시지 조회에서는 하위 Flow와 Router의 동일 최종 답변이 서로 다른 `flow_id`로 각 1건 저장된다. 이는 Router Playground의 질문 echo와 구분하며, 하위 Flow의 후속질문 session 계약을 유지하기 위해 저장 억제나 session 분리는 적용하지 않는다.
