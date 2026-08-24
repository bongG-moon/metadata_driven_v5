# CUBE 상호작용 Callback과 오류 답변

## 현재 서버의 범위

현재 GAIA-CUBE 서버는 하나의 동기 흐름만 실행한다.

```text
CUBE callback
  -> 사용자·채널·질문 확인
  -> GAIA 실행
  -> 최종 답변 추출
  -> CUBE Rich Notification 발송
  -> callback ACK 반환
```

수신 경로는 아래 하나다.

```text
POST /api/v1/receiver
```

등록된 HCP URL은 다음과 같다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

원본 FastAPI 예시의 `/api/qna`는 과거 예시일 뿐, 현재 서버에는 없다.

## 일반 텍스트와 버튼·라디오 값

일반 질문은 `process.processdata`에서 읽는다.

```json
{
  "richnotificationmessage": {
    "process": {
      "processdata": "오늘 생산 현황을 알려줘"
    }
  }
}
```

Rich Message 버튼 또는 라디오를 누르면 `processdata`가 비어 있고 선택값이 별도 key로 올 수 있다. 현재 서버는 제공된 예시에 있는 `UserSelection`, `SendBtn` 값을 질문으로 사용한다.

```json
{
  "richnotificationmessage": {
    "process": {
      "processdata": "",
      "UserSelection": "2",
      "SendBtn": "submit"
    }
  }
}
```

상호작용형 메시지를 나중에 발송한다면 `callbackaddress`에도 같은 HCP callback URL을 넣어야 한다.

```json
{
  "callbacktype": "url",
  "callbackaddress": "http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver"
}
```

## Hello 제어 메시지

CUBE가 처음 챗봇을 열 때 아래 값을 보낼 수 있다.

```text
!@#HelloChatBot#@!
```

이 값은 질문이 아니므로 GAIA와 CUBE 발송을 호출하지 않고 `{"status":"ignored"}`로 종료한다.

## 오류 시 사용자에게 보이는 내용

GAIA 호출 또는 GAIA 답변 추출에 실패하면, 서버는 같은 사용자와 채널로 아래와 같은 고정 오류 문구를 CUBE 발송 API로 보낸다.

```text
요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.
```

문구는 `.env`의 `USER_ERROR_MESSAGE`로 변경할 수 있다. callback HTTP 응답은 CUBE 시스템에 처리 결과를 알려주는 ACK이며, 실제 답변 또는 오류 문구는 별도의 CUBE Rich Notification 발송으로 채팅창에 표시된다.

## 현재 포함하지 않는 기능

현재 단계에서는 worker, 데이터베이스, outbox, 자동 재시도, 중복 요청 처리, 별도 발송 API를 추가하지 않는다. CUBE의 공식 callback timeout·재전송 정책이 확인되면 그때 필요한 기능만 추가한다.

원본 예시는 `source/` 폴더에 보존되어 있으며, 그 안의 옛 경로는 현재 실행 경로가 아니다.
