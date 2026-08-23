# 처음부터 이해하는 GAIA-CUBE 기본 연동

## 이 문서의 범위

이 문서는 API를 처음 사용하는 사람도 현재 만들려는 기본 연동을 이해할 수 있게 설명한다. 여기에는 사용자가 제공한 GAIA·CUBE 예시 코드와 지금까지 정한 정책만 담았다.

현재 기준은 다음과 같다.

1. 사용자는 CUBE 채널에서 질문한다.
2. 우리 서버는 CUBE callback으로 질문을 받는다.
3. 서버는 채널에 고정된 GAIA Agent로 질문을 전달한다.
4. GAIA의 최종 답변을 같은 CUBE 사용자와 채널에 다시 보낸다.
5. 사용자와 CUBE 채널별로 GAIA `session_id`를 구분한다.

MongoDB, worker, outbox, 재시도 정책, 스케줄 실행 구조와 대화 이력 저장 방식은 아직 이 문서에서 정하지 않는다. 나중에 필요해질 때 별도로 결정한다.

## 1. 전체 구조

이 시스템을 상담 창구에 비유하면 이해하기 쉽다.

| 실제 시스템 | 쉬운 비유 | 하는 일 |
| --- | --- | --- |
| CUBE | 질문을 입력하는 창구 | 사용자의 질문을 받고 답변을 보여준다. |
| GAIA/Langflow Agent | 답을 만드는 상담원 | 질문을 분석하고 답변을 만든다. |
| GAIA-CUBE 서버 | 전달 담당자 | CUBE 질문을 GAIA에 보내고 답변을 다시 CUBE로 보낸다. |

```text
사용자
  ↓ CUBE 채널에 질문
CUBE
  ↓ callback
우리 서버
  ↓ GAIA API 호출
GAIA의 Langflow Agent
  ↓ 최종 답변
우리 서버
  ↓ CUBE 메시지 발송 API 호출
CUBE
  ↓
같은 사용자와 채널에 답변 표시
```

## 2. API와 callback의 뜻

API는 프로그램끼리 정해진 형식으로 요청과 응답을 주고받는 방법이다.

| 용어 | 쉬운 의미 |
| --- | --- |
| URL | 요청을 보낼 주소 |
| `POST` | 데이터를 보내는 요청 방식 |
| Header | 인증이나 보내는 사람 정보 |
| JSON Body | 실제 전달할 데이터 |
| Response | 상대가 돌려주는 처리 결과 |

이 연동에는 세 번의 통신이 있다.

1. CUBE가 우리 서버에 사용자 질문을 보낸다.
2. 우리 서버가 GAIA에 질문을 보낸다.
3. 우리 서버가 CUBE에 GAIA 답변을 보낸다.

callback은 CUBE가 먼저 우리 서버를 호출하는 방식이다. 사용자가 CUBE에서 메시지를 보내면, CUBE는 등록된 서버 주소로 `POST` 요청을 보낸다.

```text
CUBE → POST /api/qna → 우리 서버
```

## 3. CUBE callback에서 받는 정보

제공된 예시에서 CUBE callback은 다음 정보를 전달한다.

- 누가 질문했는지
- 어느 채널에서 질문했는지
- 사용자가 입력한 문장
- Rich Message에서 선택한 버튼 또는 라디오 값

아래 JSON은 제공된 callback 예시들을 이해하기 쉽게 한 화면에 합친 축약 예시다. key 이름은 제공 코드에서 왔지만, 실제 callback이 항상 이 모양 하나로만 들어온다고 확정하면 안 된다.

```json
{
  "richnotificationmessage": {
    "header": {
      "from": {
        "uniquename": "EMPLOYEE_ID_EXAMPLE"
      },
      "to": {
        "channelid": [
          "CHANNEL_ID_EXAMPLE"
        ]
      }
    },
    "process": {
      "processdata": "오늘 생산 현황을 알려줘",
      "userId": "EMPLOYEE_ID_EXAMPLE",
      "channelId": "CHANNEL_ID_EXAMPLE"
    }
  }
}
```

| 필요한 정보 | 제공 예시에서 확인된 경로 |
| --- | --- |
| 사용자 | `header.from.uniquename` 또는 `process.userId` |
| 채널 | `header.to.channelid[0]` 또는 `process.channelId` |
| 일반 질문 | `process.processdata` |
| 버튼·라디오 선택 | `process.UserSelection`, `process.SendBtn` 등 |

`channelid`와 `channelId`처럼 대소문자가 다른 key는 JSON에서 서로 다른 값이다. 두 경로에 사용자나 채널 값이 함께 오면, 실제 구현에서는 두 값이 같은지 확인해야 한다.

`!@#HelloChatBot#@!`는 제공 예시에 있는 최초 진입용 값이다. 일반 질문이 아니므로 GAIA에 전달하지 않는다.

## 4. CUBE callback 응답과 실제 사용자 메시지

제공된 FastAPI 예시는 다음 순서로 동작한다.

```text
1. CUBE callback을 받는다.
2. 요청을 처리한다.
3. send_cube_message(...)로 CUBE Rich Notification API를 호출한다.
4. callback 요청에는 status/message 처리 결과를 반환한다.
```

즉, callback HTTP 응답과 사용자의 CUBE 채팅창에 표시되는 실제 메시지는 서로 다른 통신이다.

```text
callback HTTP 응답
  = CUBE 시스템에 처리 결과를 반환

CUBE Rich Notification 발송
  = 사용자의 채팅창에 메시지를 표시
```

제공된 예시에는 성공 시 `status: success`, 최초 진입 시 `status: ignored`가 나온다. 하지만 CUBE의 공식 허용 상태값, HTTP status와 timeout 규칙은 아직 확인되지 않았으므로 이 값을 현재 서버의 확정 규칙으로 고정하지 않는다.

## 5. 현재 만들 기본 질문 흐름

GAIA와 CUBE 예시를 연결하면, 기본 질문 한 건은 아래 순서로 처리된다.

### 1단계: CUBE가 질문을 보낸다

사용자가 CUBE 채널에 질문하면 CUBE가 우리 서버의 callback 주소로 JSON을 보낸다.

### 2단계: 서버가 사용자·채널·질문을 읽는다

서버는 callback JSON에서 다음 값을 읽는다.

```text
사용자 = header.from.uniquename 또는 process.userId
채널   = header.to.channelid[0] 또는 process.channelId
질문   = process.processdata
```

버튼이나 라디오 선택은 `processdata`가 비어 있어도 `UserSelection` 같은 선택 key가 있을 수 있다.

### 3단계: 채널에 연결된 GAIA Agent를 찾는다

현재 정한 정책은 다음과 같다.

```text
CUBE 채널 1개 → GAIA Agent 1개
```

따라서 사용자가 GAIA Agent를 고르는 것이 아니라, 서버가 callback의 채널 ID를 보고 미리 설정된 GAIA `svc_id`를 찾는다. `svc_id`는 callback payload에서 받지 않는다.

예를 들면 다음과 같다.

```text
생산 분석 채널 → 생산 분석 GAIA Agent
품질 분석 채널 → 품질 분석 GAIA Agent
```

### 4단계: 해당 대화의 GAIA session_id를 찾는다

세션은 “이 질문들이 같은 대화에 속한다”는 것을 나타내는 번호다.

현재 세션을 구분하는 기준은 다음과 같다.

```text
환경/tenant + 사용자 + CUBE 채널/thread
```

같은 채널이 여러 사람이 참여하는 채널일 수 있으므로 사용자도 함께 구분한다. 이렇게 하면 다른 사람의 이전 질문이 내 질문의 문맥으로 섞이지 않는다.

GAIA에 보내는 `session_id`에는 사번을 그대로 넣지 않고, 서버가 만든 불투명한 값(예: `gc_<random UUID>`)을 사용한다.

### 5단계: 서버가 GAIA에 질문을 보낸다

GAIA API에는 현재 질문, 권한 확인용 사용자 ID와 세션 ID를 보낸다.

```json
{
  "message": "오늘 생산 현황을 알려줘",
  "user_id": "EMPLOYEE_ID_EXAMPLE",
  "session_id": "gc_OPAQUE_SESSION_ID"
}
```

GAIA 호출 header의 `X-Gaia-User-Id`와 body의 `user_id`에는 같은 권한 있는 사용자 사번을 사용한다.

### 6단계: GAIA의 최종 답변만 꺼낸다

GAIA 응답 JSON에는 실행 정보와 답변이 여러 곳에 있을 수 있다. 전체 JSON을 그대로 CUBE에 보내지 않고, 마지막 유효한 Chat Output의 답변 문자열만 사용한다.

제공된 응답 예시에서 우선 확인할 위치는 다음과 같다.

```text
results.gaia_response.data.answer
```

실제 구현 전에 여러 정상 응답으로 이 위치와 마지막 Chat Output 판단 규칙을 확인한다.

### 7단계: 서버가 CUBE에 최종 답변을 보낸다

서버는 제공된 CUBE Rich Notification 발송 API로 답변을 보낸다.

```text
받는 사람 = 처음 질문한 사용자
받는 채널 = 처음 질문한 채널
표시할 내용 = GAIA의 최종 답변
```

사용자는 처음 질문했던 CUBE 채널에서 답변을 보게 된다.

### 8단계: callback 처리 결과를 반환한다

제공된 FastAPI 샘플처럼 CUBE 발송 처리 뒤 callback 요청에 `status`와 `message`를 반환한다. 실제 사용해야 하는 응답값은 CUBE 공식 가이드로 확인한 뒤 정한다.

## 6. 세션이 왜 필요한가

사용자가 이어서 질문할 수 있기 때문이다.

```text
사용자: 어제 공정별 생산량을 알려줘.
Agent: A 공정은 100개, B 공정은 200개입니다.
사용자: 그중 가장 높은 공정은?
```

두 번째 질문의 “그중”을 이해하려면 첫 번째 대화와 같은 GAIA `session_id`를 사용해야 한다.

사용자 테스트에서 같은 GAIA `session_id`를 사용하면 대화가 계속 누적되는 동작이 확인되었다. 그래서 사용자별로 하나의 사번만 세션 ID로 쓰면 안 되고, 사용자와 CUBE 채널을 함께 구분해야 한다.

사용자는 전체 대화를 무제한으로 보관하지 않고 최근 몇 턴만 확인하고 싶다는 요구가 있다. 다만 최근 몇 턴을 어디에, 얼마나 오래 저장하고 조회할지는 아직 기본 API 연동 단계에서 정하지 않는다. 제공된 GAIA API에는 과거 세션 대화를 조회하는 endpoint가 보이지 않는다.

## 7. 실제 연동과 더미 연동

실제 운영 API 정보가 아직 완전하지 않을 수 있으므로 두 가지 실행 방식을 구분한다.

| 방식 | GAIA | CUBE |
| --- | --- | --- |
| 실제 연동 | 실제 GAIA API 호출 | 실제 CUBE 발송 API 호출 |
| 더미 연동 | 미리 준비한 GAIA 응답 사용 | 실제 발송 없이 만들 payload만 확인 |

두 방식은 같은 callback JSON을 읽고, 같은 세션 기준과 같은 답변 추출 규칙을 사용해야 한다. 실제 연동에 필요한 URL, 인증 키, 사용자 권한이 없으면 더미 연동으로 테스트할 수 있지만, 운영 설정이 잘못됐을 때 자동으로 더미 답변을 사용자에게 보내면 안 된다.

## 8. 구현 전에 CUBE에 확인할 항목

현재 제공된 예시만으로 확정할 수 없는 항목이다.

- callback의 공식 URL과 인증/서명 방식
- callback 응답의 공식 `status` 값과 HTTP status
- callback timeout과 재전송 규칙
- 실제 message/event ID의 위치
- `userId`, `channelId`, `cubeuniquename`, `cubechannelid`의 공식 규칙
- CUBE 발송 API의 성공·오류 응답 형식
- CUBE 메시지 최대 길이

이 정보가 확인되면 실제 API 호출 부분을 그 계약에 맞게 구현한다.

## 9. 용어 정리

| 용어 | 쉬운 설명 |
| --- | --- |
| API | 프로그램끼리 정해진 형식으로 요청과 응답을 주고받는 방법 |
| Callback | CUBE가 우리 서버를 먼저 호출하는 API 요청 |
| Session | 같은 대화에 속한 질문과 답변의 묶음 |
| `svc_id` | GAIA에서 호출할 Agent를 가리키는 식별값 |
| Rich Notification | CUBE 사용자 채팅창에 실제 메시지를 표시하는 발송 형식 |
| Dummy | 실제 외부 API를 호출하지 않는 테스트용 구현 |

## 함께 읽을 사용자 제공 가이드

- [`base_guide/gaia/01_external_langflow_api.md`](base_guide/gaia/01_external_langflow_api.md): GAIA 호출 형식
- [`base_guide/gaia/02_response_extraction.md`](base_guide/gaia/02_response_extraction.md): GAIA 응답에서 답변을 꺼내는 예시
- [`base_guide/cube/01_message_send_api.md`](base_guide/cube/01_message_send_api.md): CUBE Rich Notification 발송 형식
- [`base_guide/cube/02_callback_api.md`](base_guide/cube/02_callback_api.md): CUBE callback 수신 예시
- [`base_guide/cube/03_fastapi_rich_message_and_fallback.md`](base_guide/cube/03_fastapi_rich_message_and_fallback.md): 제공 FastAPI 예시와 Rich Message 입력 예시
