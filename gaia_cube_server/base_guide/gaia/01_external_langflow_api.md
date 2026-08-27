# GAIA 외부 Langflow 실행 API

## 현재 서버에서 사용하는 방법

GAIA-CUBE callback 서버는 `.env`에 입력한 **완성된 GAIA API URL**을 그대로 호출한다. 코드가 base URL과 Agent 서비스 ID를 조합하지 않는다.

```dotenv
GAIA_API_URL=http://gaia.api.skhynix.com/v2/agents/실제_AGENT_SVC_ID/external
GAIA_AUTH_KEY=GAIA에서_발급받은_키
```

즉, `GAIA_API_URL` 한 값 안에 `/v2/agents/.../external` 전체 경로가 들어 있어야 한다.

## 요청 형식

```text
POST <GAIA_API_URL>
Content-Type: application/json
X-Gaia-Auth-Key: <GAIA_AUTH_KEY>
X-Gaia-User-Id: <CUBE에서 받은 사용자 ID>
```

```json
{
  "input_value": "사용자가 CUBE에 입력한 질문",
  "user_id": "CUBE에서 받은 사용자 ID",
  "session_id": "현재 사용자와 채널의 GAIA 세션 ID",
  "tweaks": {
    "GaiA Input": {
      "data": "{\"conversation_history\":[{\"role\":\"user\",\"content\":\"사용자가 CUBE에 입력한 질문\",\"files\":[]}]}",
      "metadata": "{\"platform\":\"CUBE\",\"user_id\":\"CUBE에서 받은 사용자 ID\",\"session_id\":\"현재 사용자와 채널의 GAIA 세션 ID\",\"cube_channel_id\":\"CUBE 채널 ID\"}"
    }
  }
}
```

현재 실제 Agent에서 `input_value`가 정상 전달되는 것을 확인했으므로 서버는 질문을 이 key로 보낸다. `X-Gaia-User-Id`와 JSON의 `user_id`에는 같은 값을 넣는다. `data`와 `metadata`는 최상위 body가 아니라 고정된 **`tweaks["GaiA Input"]`**에 넣어야 GaiA Input 값으로 들어간다. `metadata.user_id`, `metadata.session_id`는 CUBE 연동의 필수 값이다.

## Python 최소 예시

```python
import requests

response = requests.post(
    "http://gaia.api.skhynix.com/v2/agents/실제_AGENT_SVC_ID/external",
    headers={
        "Content-Type": "application/json",
        "X-Gaia-Auth-Key": "발급받은_키",
        "X-Gaia-User-Id": "권한이_있는_사번",
    },
    json={
        "input_value": "오늘 생산 현황을 알려줘",
        "user_id": "권한이_있는_사번",
        "session_id": "TEST_0824",
        "tweaks": {
            "GaiA Input": {
                "data": json.dumps(
                    {
                        "conversation_history": [
                            {
                                "role": "user",
                                "content": "오늘 생산 현황을 알려줘",
                                "files": [],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                "metadata": json.dumps(
                    {
                        "platform": "CUBE",
                        "user_id": "권한이_있는_사번",
                        "session_id": "TEST_0824",
                        "cube_channel_id": "CUBE_채널_ID",
                    },
                    ensure_ascii=False,
                ),
            }
        },
    },
    timeout=10,
)
response.raise_for_status()
```

## 세션 사용 방식

같은 `session_id`로 GAIA를 반복 호출하면 대화가 이어질 수 있다. 현재 callback 서버는 **사용자 ID + CUBE 채널 ID**별로 GAIA session ID 하나를 메모리에 보관하고 다음 질문에 재사용한다.

GAIA가 응답 루트의 `session_id`를 반환하면 서버는 그 값을 다음 호출부터 사용한다. 서버가 재시작되면 메모리 세션은 사라져 새 대화로 시작한다.

## 주의

- GAIA 인증 키를 코드, Git, 문서 예시, 로그에 실제 값으로 넣지 않는다.
- GAIA URL은 사내망에서 접근 가능한 전체 API 주소를 입력한다.
- 전체 응답 JSON을 CUBE로 보내지 않는다. 최종 답변만 추출하는 방법은 `02_response_extraction.md`를 따른다.
