# CUBE Rich Notification 메시지 발송 API

## 출처와 범위

- 출처: 사용자 제공 `CUBE개발 관련 코드`의 메시지 발송 예시
- 수신일: 2026-08-22
- 용도: 봇이 한 명 이상의 CUBE 사용자와 채널에 텍스트 메시지를 발송한다.
- 원문의 들여쓰기는 복원했지만 필드명과 계층은 제공된 예시를 유지했다.

## Endpoint

```text
POST http://cube.example.internal/legacy/richnotification
Content-Type: application/json
```

- 제공된 값은 **CUBE 개발 서버 주소**다.
- 운영 주소, DNS, HTTPS 지원 여부와 네트워크 접근 정책은 구현 전에 별도로 확인한다.

## 요청 구조

```text
richnotification
├─ header
│  ├─ from
│  ├─ token
│  └─ to
│     ├─ uniquename[]
│     └─ channelid[]
└─ content[]
   ├─ header
   ├─ body
   │  └─ row[]
   │     └─ column[]
   │        ├─ type = "label"
   │        └─ control.text[]
   └─ process
```

## 주요 필드

| 경로 | 형식 | 필수 | 의미 |
| --- | --- | --- | --- |
| `richnotification.header.from` | string | 예 | 메시지를 보내는 봇 사번/ID |
| `richnotification.header.token` | string | 예 | 봇 인증 토큰 |
| `richnotification.header.to.uniquename` | string[] | 예 | 메시지를 받을 사용자 사번 목록 |
| `richnotification.header.to.channelid` | string[] | 예 | 메시지를 보낼 채널 ID 목록 |
| `richnotification.content` | object[] | 예 | 표시할 메시지 블록 목록 |
| `content[].body.row[].column[].type` | string | 예 | 텍스트 예시는 `label` |
| `content[].body.row[].column[].control.text` | string[] | 예 | 표시할 텍스트 목록 |
| `content[].process` | object | 예시상 포함 | 추가 동작이 없으면 빈 객체 |

추가 FastAPI 예시에서 `column[].type`으로 `image`, `radio`, `button`도 확인되었다. 상호작용형 메시지의 `process` 계약은 `03_fastapi_rich_message_and_fallback.md`를 따른다.

## 배열 형식 규칙

한 명에게 한 문장만 보내더라도 다음 필드는 반드시 배열로 전달한다.

- `uniquename`: `["12345"]`
- `channelid`: `["channel_01"]`
- `control.text`: `["안녕하세요"]`

## 정규화한 JSON 예시

전체 예시는 `examples/send_message.request.json`에 저장했다.

```json
{
  "richnotification": {
    "header": {
      "from": "YOUR_BOT_ID",
      "token": "YOUR_BOT_TOKEN",
      "to": {
        "uniquename": ["TARGET_USER_ID"],
        "channelid": ["TARGET_CHANNEL"]
      }
    },
    "content": [
      {
        "header": {},
        "body": {
          "bodystyle": "none",
          "row": [
            {
              "bgcolor": "#ffffff",
              "border": false,
              "align": "",
              "width": "",
              "column": [
                {
                  "bgcolor": "#ffffff",
                  "border": false,
                  "align": "",
                  "valign": "middle",
                  "width": "100%",
                  "type": "label",
                  "control": {
                    "active": true,
                    "text": ["안녕하세요! Python 봇이 보낸 테스트 메시지입니다."],
                    "color": "#000000"
                  }
                }
              ]
            }
          ]
        },
        "process": {}
      }
    ]
  }
}
```

## 들여쓰기를 복원한 Python 예시

아래 코드는 제공된 예시를 읽기 쉬운 형태로 복원한 참고 코드다. 실제 서버 구현은 설정 객체와 실제/더미 CUBE 어댑터를 분리한다.

```python
import requests


def send_chatops_message(
    *,
    api_url: str,
    bot_id: str,
    bot_token: str,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> requests.Response:
    payload = {
        "richnotification": {
            "header": {
                "from": bot_id,
                "token": bot_token,
                "to": {
                    "uniquename": [receiver_id],
                    "channelid": [channel_id],
                },
            },
            "content": [
                {
                    "header": {},
                    "body": {
                        "bodystyle": "none",
                        "row": [
                            {
                                "bgcolor": "#ffffff",
                                "border": False,
                                "align": "",
                                "width": "",
                                "column": [
                                    {
                                        "bgcolor": "#ffffff",
                                        "border": False,
                                        "align": "",
                                        "valign": "middle",
                                        "width": "100%",
                                        "type": "label",
                                        "control": {
                                            "active": True,
                                            "text": [message_text],
                                            "color": "#000000",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    "process": {},
                }
            ],
        }
    }
    response = requests.post(
        api_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    return response
```

원문은 `data=json.dumps(payload)`를 사용한다. 구현에서는 같은 JSON을 보내는 `json=payload`를 사용할 수 있다.

## callback 요청자에게 답변할 때의 매핑

추가 FastAPI callback 예시를 기준으로 다음 매핑을 사용한다.

| callback 요청 | 발송 요청 |
| --- | --- |
| `richnotificationmessage.header.from.uniquename` | `richnotification.header.to.uniquename[0]` |
| `richnotificationmessage.header.to.channelid[0]` | `richnotification.header.to.channelid[0]` |
| GAIA에서 추출한 최종 답변 | `content[0].body.row[0].column[0].control.text[0]` |

Rich Message 상호작용 callback의 `process.userId`와 `process.channelId`도 후보 값을 제공한다. 기본적으로 header 값을 신뢰 경계로 사용하고, process 값이 함께 오면 서로 일치하는지 검사한다. CUBE의 callback 인증 방식이 확인되기 전에는 어느 값도 인증된 사용자 정보로 단독 신뢰하지 않는다.

## 응답과 성공 판정

제공된 코드는 HTTP 200을 성공으로 처리하고 `response.text`를 출력한다. 공식 성공 응답 JSON과 애플리케이션 상태 필드는 제공되지 않았으므로 다음을 아직 가정하지 않는다.

- 모든 2xx가 성공인지 여부
- HTTP 200 안의 별도 성공/실패 상태
- 중복 발송 방지용 request/message ID
- 재시도 가능한 오류 코드

## 보안과 재시도 주의사항

- 봇 토큰은 HTTP header가 아니라 JSON 본문의 `richnotification.header.token`에 포함된다. 요청 body와 payload 전체를 로그에 남기지 않는다.
- 개발 endpoint가 `http://`이므로 보호된 사내망인지 확인하고 운영 환경의 HTTPS/mTLS 지원 여부를 확인한다.
- timeout을 명시한다.
- 발송 API의 멱등성 키가 확인되기 전에는 timeout 후 자동 재시도가 같은 메시지를 중복 발송할 수 있다.
- timeout은 확정 실패가 아니라 CUBE가 요청을 받았는지 알 수 없는 `unknown delivery`일 수 있다. 이 경우 즉시 정상 메시지나 fallback을 다시 발송하지 않고 outbox에서 별도 상태로 관리한다.
- callback에서 받은 사용자·채널 값을 인증 없이 그대로 발송 대상으로 사용하지 않는다.

## 구현 전 확인할 항목

- 운영 endpoint
- 봇 ID/토큰 발급·교체 정책
- 공식 성공 및 오류 응답 schema
- 최대 수신자·채널·텍스트 길이
- 여러 `uniquename`과 `channelid`가 같은 인덱스끼리 대응하는지, 독립 대상 집합인지 여부
- 여러 수신자 중 일부만 실패할 때의 응답 형식
- Markdown, 링크, 파일 및 여러 column 지원 범위
- 발송 멱등성 또는 CUBE message ID 지원 여부
- `align`과 `width` 빈 문자열 허용 여부
