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

제공받은 예시와 실제 실행 중인 CUBE callback 코드에서는 사용자·채널 정보가 `header`와 `process`의 여러 위치에 올 수 있다.

```json
{
  "richnotificationmessage": {
    "header": {
      "from": {
        "uniquename": "EMPLOYEE_ID_EXAMPLE",
        "channelid": "CHANNEL_ID_EXAMPLE"
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

| 의미 | 읽는 위치 | 처리 규칙 |
| --- | --- | --- |
| 질문 | `process.processdata` | 비어 있으면 `process.UserSelection`, `process.SendBtn`, `result.resultdata[].value` 순서로 확인 |
| 사용자 사번/ID | `header.from.uniquename`, `process.userId` | 둘 다 있으면 같은 값이어야 함 |
| CUBE 채널 ID | `header.from.channelid`, `header.to.channelid`, `process.channelId` | 들어온 모든 값이 같은 하나의 채널이어야 함 |

`channelid`는 문자열 또는 배열로 올 수 있다. 배열 안에 서로 다른 채널 ID가 있거나, `header`와 `process`의 사용자·채널 값이 서로 다르면 서버는 질문을 GAIA에 보내지 않고 HTTP 400을 반환한다. 첫 번째 값만 골라 답장하지 않는 이유는 다른 CUBE 채팅방으로 답변이 갈 위험을 막기 위해서다.

`result.resultdata[].value`는 버튼·라디오·체크박스의 선택 결과가 들어오는 실제 callback 형태다. 일반 텍스트 `processdata`가 있으면 그것이 항상 우선한다. `value`에 여러 개의 텍스트 선택값이 있으면 서버는 중복을 제거한 뒤 줄바꿈으로 연결해 GAIA에 전달하며, 숫자나 JSON 객체를 임의로 문자열로 바꾸지 않는다.

## 서버의 처리 순서

```text
1. CUBE가 POST /api/v1/receiver 호출
2. 서버가 질문·사용자·채널 값을 확인
3. 유효한 요청이면 callback에 즉시 HTTP 200 + JSON null ACK 반환
4. 응답 뒤 FastAPI 백그라운드 작업이 사용자 + 채널의 GAIA session_id를 찾거나 새로 생성
5. .env의 GAIA_API_URL로 질문 전달하고 최종 answer 추출
6. CUBE 발송 API로 실제 답변 또는 fallback 안내를 전송
```

동일한 사용자와 동일한 채널은 GAIA가 반환한 `session_id`를 다음 질문에 다시 사용한다. 서버를 재시작하면 이 메모리 세션은 초기화된다.

## ACK와 실제 답변의 차이

callback HTTP 응답은 CUBE 시스템에 “요청을 접수했다”라고 알려 주는 ACK다. GAIA 실행과 CUBE 발송 완료를 기다리지 않는다.

```json
null
```

이 `null`은 사용자 채팅창에 보이는 답변이 아니다. 실제 답변은 ACK 뒤에 서버가 CUBE Rich Notification 발송 API를 별도로 호출해 표시한다. 따라서 GAIA 또는 CUBE 발송 오류는 이미 반환한 callback HTTP 상태를 바꾸지 못하며, 서버 로그와 CUBE fallback 발송 결과로 확인한다.

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
