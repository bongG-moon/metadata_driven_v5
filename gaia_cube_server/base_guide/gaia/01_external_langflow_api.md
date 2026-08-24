# GAIA 외부 Langflow 실행 API

## 출처와 범위

- 출처: 사용자 제공 GAIA API 코드 예시
- 수신일: 2026-08-22
- 용도: GAIA 환경에서 실행되는 Langflow Agent에 메시지를 보내고 실행 결과를 받는다.

## Endpoint

```text
POST http://gaia.example.internal/v2/agents/{svc_id}/external
```

- `svc_id`: 호출할 GAIA Agent 서비스 식별자
- 제공된 가이드의 URL scheme은 `http`이다. 실제 운영 배포 시에도 임의로 변경하지 않고 운영 가이드와 네트워크 정책을 다시 확인한다.

## Headers

| 이름 | 값 | 필수 | 비고 |
| --- | --- | --- | --- |
| `Content-Type` | `application/json` | 예 | JSON 요청 |
| `X-Gaia-Auth-Key` | GAIA에서 발급받은 키 | 예 | 코드·문서·로그에 실제 값을 저장하지 않는다. |
| `X-Gaia-User-Id` | 권한이 있는 사용자 사번 | 예 | 본문의 `user_id`와 같은 값을 사용한다. |

## 현재 Agent에서 확인된 JSON Body

초기 제공 코드 예시는 질문 key로 `message`를 사용했다. 그러나 2026-08-24 실제 Agent 호출에서는 `input_value`를 전송했을 때 정상적으로 Flow 입력과 응답이 확인되었다. 따라서 현재 `production_callback_server`는 **`input_value`만** 전송한다. 같은 요청에 이전 `message`와 `input_value`를 함께 넣지 않는다.

| 필드 | 형식 | 필수 | 의미 |
| --- | --- | --- | --- |
| `input_value` | string | 예 | 현재 확인된 Langflow Agent의 사용자 질문 입력 |
| `user_id` | string | 예 | 권한 있는 사용자 ID. `X-Gaia-User-Id`와 동일해야 한다. |
| `session_id` | string | 예 | Chat completion을 묶는 대화 단위 식별자 |

## 호출 예시

```python
import requests

svc_id = "XXXXXX"
gaia_key = "발급받은 키"
user_id = "권한 있는 사용자 사번"

url = f"http://gaia.example.internal/v2/agents/{svc_id}/external"
headers = {
    "Content-Type": "application/json",
    "X-Gaia-Auth-Key": gaia_key,
    "X-Gaia-User-Id": user_id,
}
payload = {
    "input_value": "전송할 메시지",
    "user_id": user_id,
    "session_id": "Chat completion 단위",
}

response = requests.post(url, headers=headers, json=payload, timeout=10)
response.raise_for_status()
body = response.json()
```

사용자가 제공한 예시는 `data=json.dumps(payload)`를 사용한다. 구현에서는 동일한 JSON 요청을 더 직접적으로 표현하는 `json=payload`를 사용할 수 있다.

## 구현 시 확정된 규칙

1. `svc_id`, 인증 키와 사용자 ID는 코드에 하드코딩하지 않는다.
2. 현재 확인된 Agent에는 질문을 `input_value`로 보낸다. 다른 입력 key를 요구하는 Agent를 추가할 때만 그 Agent의 실제 계약을 확인해 변경한다.
3. `X-Gaia-User-Id`와 `user_id`는 항상 같은 값으로 보낸다.
4. 대화 연속성이 필요한 요청은 동일한 `session_id`를 재사용하고, GAIA가 루트 `session_id`를 돌려주면 다음 요청에 그 값을 사용한다.
5. HTTP 성공 여부와 JSON 파싱 성공 여부를 각각 검사한다.
6. 응답 전체 JSON을 CUBE 메시지로 전달하지 않는다. 최종 답변 문자열 추출 계약은 `02_response_extraction.md`를 따른다.

## 사용자 실행으로 확인된 동작

- 동일한 `session_id`로 여러 번 호출하면 대화 내용이 계속 누적되는 동작을 사용자가 확인했다.
- 다만 GAIA가 저장·조회하는 정확한 최대 범위, 만료 시간 및 삭제 정책은 아직 확인되지 않았다.
- 따라서 `session_id`를 사용자별로 무기한 고정하지 않고, 서버의 논리적 채팅 세션과 현재 활성 GAIA 세션 generation을 분리한다.
- 최근 대화 제한과 세션 교체 기준은 `../common/01_conversation_session_and_recent_memory.md`를 따른다.

## 아직 미확정인 항목

- 200 이외 상태 코드별 오류 응답 JSON
- 요청 제한, 재시도 및 권장 timeout
- 동기 응답 이외의 비동기/callback 지원 여부
- 인증 키 교체 및 만료 정책
- `session_id`의 허용 길이, 문자 및 만료 정책

## 전송 보안 확인

제공된 endpoint는 `http://`이다. 인증 키, 사번 및 메시지가 포함되므로 운영 구현 전에 해당 구간이 보호된 사내망인지 확인하고, GAIA가 지원한다면 HTTPS 또는 mTLS 사용 가능 여부를 확인한다. 확인 전에는 인터넷이나 신뢰되지 않은 네트워크를 통해 호출하지 않는다.
