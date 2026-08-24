# CUBE 메시지 Callback API

## 역할

CUBE 사용자가 채팅창에 입력하면 CUBE가 우리 서버로 HTTP POST 요청을 보낸다. 이 요청을 받는 주소가 callback URL이다.

현재 등록한 callback URL과 서버 route는 아래 하나다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
POST /api/v1/receiver
```

원본 예시의 `/api/qna`, `/api/chatops/callback`은 참고용 과거 경로이며 현재 서버에 없다.

## CUBE가 전달하는 값

제공받은 예시에서는 사용자·채널 정보가 `header`와 `process` 양쪽에 올 수 있다.

```json
{
  "richnotificationmessage": {
    "header": {
      "from": {
        "uniquename": "EMPLOYEE_ID_EXAMPLE"
      },
      "to": {
        "channelid": ["CHANNEL_ID_EXAMPLE"]
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

| 의미 | 우선 확인 경로 | 함께 있으면 하는 확인 |
| --- | --- | --- |
| 질문 | `richnotificationmessage.process.processdata` | 빈 값이면 `UserSelection`, `SendBtn` 확인 |
| 사용자 사번/ID | `header.from.uniquename` | `process.userId`와 같은지 확인 |
| CUBE 채널 ID | `header.to.channelid[0]` | `process.channelId`와 같은지 확인 |

header와 process의 사용자 또는 채널 값이 서로 다르면 서버는 질문을 GAIA에 보내지 않고 HTTP 400을 반환한다.

## 서버의 처리 순서

```text
1. CUBE가 POST /api/v1/receiver 호출
2. 서버가 질문·사용자·채널 값을 확인
3. 사용자 + 채널의 GAIA session_id를 메모리에서 찾거나 새로 생성
4. .env의 GAIA_API_URL로 질문 전달
5. GAIA 응답의 최종 answer 추출
6. CUBE 발송 API로 답변 전송
7. callback 요청에는 ACK JSON 반환
```

동일한 사용자와 동일한 채널은 GAIA가 반환한 `session_id`를 다음 질문에 다시 사용한다. 서버를 재시작하면 이 메모리 세션은 초기화된다.

## ACK와 실제 답변의 차이

callback HTTP 응답은 CUBE 시스템에 “처리가 끝났다”라고 알려 주는 ACK다.

```json
{
  "status": "success",
  "message": "GAIA answer was sent to CUBE."
}
```

이 JSON의 `message`가 사용자 채팅창에 보이는 답변은 아니다. 실제 답변은 서버가 CUBE Rich Notification 발송 API를 별도로 호출해 표시한다.

## Hello 제어 요청

`processdata`가 `!@#HelloChatBot#@!`이면 챗봇 진입 제어 요청으로 보고 GAIA를 실행하지 않는다.

```json
{
  "status": "ignored"
}
```

## 아직 확인이 필요한 CUBE 정책

- callback 인증 또는 서명 방식
- callback timeout과 재전송 조건
- CUBE 발송 API의 공식 오류/성공 JSON
- message ID 또는 중복 요청 식별값

이 값들은 사용자 제공 가이드에 확정되어 있지 않아 현재 최소 구현에는 넣지 않았다.
