# FastAPI Rich Message와 Fallback 계약

## 출처와 범위

- 출처: 사용자 제공 FastAPI 기반 CUBE 메시지 수신·fallback 예시
- 수신일: 2026-08-22
- 예시 경로: `src/api/callback.py`, `POST /qna`
- 실제 외부 경로: router가 `/api` prefix로 등록되면 `POST /api/qna`

이 문서는 예시의 의도를 보존하면서 실제 `gaia_cube_server`에서 안전하게 구현하기 위한 보완 계약을 정의한다. 아직 서버 코드는 구현하지 않는다.

## 확인된 전체 흐름

```text
사용자가 CUBE에 메시지 입력
  -> CUBE가 FastAPI callback으로 POST
  -> 서버가 CUBE 사용자·채널·입력을 검증
  -> 일반 질문이면 GAIA/Langflow 실행
  -> GAIA 최종 답변 또는 Rich Message content 생성
  -> 서버가 CUBE Rich Notification API를 별도 호출
  -> CUBE가 사용자에게 메시지 표시
  -> callback 호출에는 처리 ACK 반환
```

Rich Message의 라디오 또는 버튼을 누르면 CUBE가 같은 callback URL을 다시 호출하며, 선택값은 `richnotificationmessage.process`에 동적 키로 전달된다.

## Rich Message column 유형

| `column[].type` | 주요 control 필드 | 의미 |
| --- | --- | --- |
| `label` | `active`, `text[]`, `color` | 일반 텍스트 |
| `image` | `active`, `text[]`, `sourceurl`, `width`, `height` | 이미지 |
| `radio` | `processid`, `active`, `text[]`, `value`, `checked` | 선택 항목 |
| `button` | `processid`, `active`, `text[]`, `value` | 전송/동작 버튼 |

이미지 URL은 CUBE 클라이언트가 접근 가능한 사내망 주소여야 한다. 외부 URL, HTTP/HTTPS 지원, 파일 크기와 형식은 아직 미확정이다.

## 상호작용 process 계약

```json
{
  "callbacktype": "url",
  "callbackaddress": "http://YOUR_SERVER/api/qna",
  "mandatory": [
    {
      "processid": "UserSelection",
      "alertmsg": [
        "옵션을 선택해주세요!"
      ]
    }
  ],
  "requestid": [
    "UserSelection",
    "SendBtn",
    "cubeuniquename",
    "cubechannelid"
  ]
}
```

- `callbacktype`: 예시에서는 `url`
- `callbackaddress`: 사용자의 상호작용 결과를 받을 callback URL
- `mandatory[].processid`: 필수 선택값의 `processid`
- `mandatory[].alertmsg`: 누락 시 표시할 문구 배열
- `requestid`: callback에서 돌려받을 process ID 및 CUBE 제공 식별값 목록
- `cubeuniquename`, `cubechannelid`: CUBE가 사용자·채널 정보를 process 값으로 전달하도록 요청하는 특수 식별자로 보인다.

예시에서는 라디오의 `processid=UserSelection`, 버튼의 `processid=SendBtn`이다. 실제로 받아야 하는 상호작용 키는 `requestid`에도 포함하는 것을 기본으로 하며, CUBE 공식 규칙으로 다시 확인한다.

## 선택 callback 예시

```json
{
  "richnotificationmessage": {
    "header": {
      "from": {
        "uniquename": "EMPLOYEE_ID_EXAMPLE",
        "username": "USER_NAME_EXAMPLE"
      },
      "to": {
        "channelid": [
          "CHANNEL_ID_EXAMPLE"
        ]
      }
    },
    "process": {
      "processdata": "",
      "userId": "EMPLOYEE_ID_EXAMPLE",
      "channelId": "CHANNEL_ID_EXAMPLE",
      "UserSelection": "2",
      "SendBtn": "submit"
    }
  }
}
```

`processid`는 대소문자를 포함해 callback의 동적 JSON 키와 정확히 일치한다. `processdata`가 비어 있어도 `UserSelection` 또는 다른 요청된 process ID가 있으면 유효한 상호작용 요청일 수 있다.

## 입력 유형 정규화

callback parser는 다음처럼 입력 유형을 구분한다.

1. `processdata == !@#HelloChatBot#@!`: `hello_handshake`
2. 알려진 process ID가 존재: `rich_interaction`
3. 비어 있지 않은 일반 `processdata`: `text_message`
4. 위 조건에 해당하지 않음: `invalid_or_unsupported`

handshake는 GAIA에 보내지 않는다. Rich interaction을 GAIA 질문으로 변환할지 내부 명령으로 처리할지는 각 `processid`의 명시적 라우팅 계약으로 결정한다.

## Fallback 정책

Fallback은 모든 오류에서 무조건 CUBE 발송을 시도하는 하나의 `except` 블록으로 구현하지 않는다.

| 실패 지점 | 사용자·채널 신뢰 가능 | 사용자 fallback 발송 | 처리 원칙 |
| --- | --- | --- | --- |
| JSON/envelope 검증 실패 | 아니요 | 하지 않음 | 400 ACK, 내부 오류만 기록 |
| callback 인증 실패 | 아니요 | 하지 않음 | 401/403, GAIA와 CUBE 호출 금지 |
| HelloChatBot sentinel | 예 | 하지 않음 | 200 ignored ACK |
| GAIA 실행 실패 | 예 | 가능 | 고정된 일반 오류 문구를 outbox로 1회 생성 |
| GAIA 응답 추출 실패 | 예 | 가능 | 실행 실패로 기록하고 일반 오류 문구 사용 |
| 주 CUBE 발송 실패 | 예 | 같은 API로 즉시 재귀 fallback 금지 | outbox retry/dead-letter 정책 적용 |
| 예상하지 못한 내부 오류 | 검증 결과에 따름 | 검증된 경우에만 가능 | 외부에는 상세 예외 비공개 |

권장 사용자 오류 문구 예시는 `요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.`처럼 내부 원인을 포함하지 않는 고정 문구다. callback 응답에 `str(exc)`를 포함하지 않고 내부 로그에는 correlation ID와 오류 분류만 남긴다.

CUBE 발송 timeout은 확정 실패가 아니라 `unknown delivery`다. CUBE가 메시지를 접수한 뒤 응답만 유실되었을 수 있으므로 즉시 fallback을 추가 발송하면 정상 답변과 오류 답변이 함께 보일 수 있다. 멱등성 정책이 확인될 때까지 별도 상태와 운영 재처리 대상으로 둔다.

정상 사용자 오류 fallback도 `examples/fallback_message.request.json`처럼 전체 Rich Notification content schema를 사용한다.

## 제공 FastAPI 예시를 구현할 때의 보완점

1. `rom fastapi`는 `from fastapi`로 복원한다.
2. `get_logger(name)`의 `name`은 정의되지 않았으므로 프로젝트 로거 계약 또는 `__name__`을 사용한다.
3. `async def` 내부에서 동기 `requests.post`를 직접 호출하면 event loop가 막힌다. 실제 어댑터는 async HTTP client를 사용하거나 동기 호출을 thread로 격리한다.
4. `Dict[str, Any]`만 받지 않고 Pydantic request model과 명시적인 parser로 list/string/빈 값 조건을 검증한다.
5. `HTTPException`을 광범위한 `except Exception`으로 다시 잡아 정상적인 400을 200 error body로 바꾸지 않는다.
6. payload 검증 전에 `user_id`와 `channel_id`를 사용하지 않는다. 검증되지 않은 route로 fallback을 보내지 않는다.
7. fallback content도 `header/body/row/column/control`을 갖춘 정상 Rich Notification content schema로 생성한다.
8. 전체 payload와 사용자 메시지를 운영 로그에 출력하지 않는다. 개발 중에도 더미 데이터 또는 redaction을 사용하고 임시 진단 로그는 제거한다.
9. CUBE HTTP 200만으로 최종 사용자 표시 완료를 단정하지 않는다.
10. callback 처리와 실제 발송 상태를 분리한다.
11. 공식 계약이 아니라면 ACK에 `targetUser`를 반사하지 않아 불필요한 사번 노출을 줄인다.

## FastAPI 계층 분리

실제 구현은 다음 경계로 나눈다.

- `CubeCallbackParser`: envelope, identity, route와 입력 유형 정규화
- `ConversationService`: 세션 조회, 순서 보장, 중복 방지
- `GaiaClient`: GAIA 실행 및 최종 응답 추출
- `CubeContentBuilder`: label/image/radio/button content 생성
- `CubeTransport`: Rich Notification HTTP 발송
- `FallbackPolicy`: 오류 분류와 사용자용 고정 메시지 결정
- `OutboxStore`: 답변·fallback의 재시도와 dead-letter 관리

운영 구현과 더미 구현은 같은 인터페이스를 사용한다.

- 운영: 실제 GAIA/CUBE HTTP client와 지속성 저장소
- 더미: fixture 기반 GAIA/CUBE client와 인메모리 저장소, 외부 네트워크 호출 없음

## 최소 검증 시나리오

1. HelloChatBot sentinel은 설정된 handshake ACK를 반환하고 GAIA/CUBE 발송 호출이 없다. 현재 샘플 값은 HTTP 200과 `ignored`다.
2. 일반 텍스트는 사용자·채널을 검증하고 올바른 세션으로 GAIA를 한 번 호출한다.
3. 라디오 선택은 빈 `processdata`에서도 `UserSelection` 값을 읽는다.
4. header와 process의 사용자 또는 채널이 다르면 요청을 거부한다.
5. 잘못된 envelope에는 fallback을 보내지 않는다.
6. GAIA timeout에는 검증된 route로 일반 오류 outbox를 한 번만 만든다.
7. CUBE 발송 timeout에는 GAIA를 재실행하지 않고 같은 outbox를 재처리한다.
8. 중복 callback에는 GAIA와 CUBE 발송을 중복 실행하지 않는다.
9. 운영/더미 어댑터에 동일한 payload contract test를 적용한다.

## 미확정 항목

- CUBE callback 인증 방식과 ACK timeout
- `requestid`, `mandatory`와 button process ID의 정확한 상호작용 규칙
- `cubeuniquename`, `cubechannelid`가 생성하는 실제 process 키 이름
- HelloChatBot ACK의 `ignore`와 `ignored` 중 공식 값
- ACK의 `targetUser` 필수 여부
- callback 재전송 시 제공되는 message/event ID
- CUBE 발송 API의 성공 body와 멱등성 지원
- Rich Message 지원 크기, row/column 개수와 이미지 정책
- callback 처리 중 비동기 202 ACK 지원 여부
