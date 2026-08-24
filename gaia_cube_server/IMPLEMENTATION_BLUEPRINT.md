# 현재 GAIA-CUBE Callback Server 구현 기준

이 문서는 현재 구현된 최소 HCP 서버의 계약을 정리한다. 향후 데이터베이스, 스케줄러, 재시도 큐 설계를 미리 적용하지 않는다.

## 1. 하나의 처리 흐름

```text
POST /api/v1/receiver
  → CUBE callback 파싱
  → 현재 GAIA session ID 조회
  → GAIA_API_URL 호출
  → 최종 답변 추출
  → CUBE Rich Notification 발송
  → callback 처리 결과 반환
```

현재는 worker 없이 위 순서를 한 HTTP 요청 안에서 동기적으로 처리한다.

등록된 HCP callback URL:

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

공개 메시지 발송 endpoint는 없다. 수동 시험도 callback payload를 이 경로에 POST해서 전체 흐름을 실행한다.

## 2. CUBE callback 입력

서버는 다음 정보를 사용한다.

```json
{
  "richnotificationmessage": {
    "process": {
      "processdata": "사용자의 질문",
      "userId": "사용자 사번",
      "channelId": "CUBE 채널 ID"
    }
  }
}
```

header에 사용자 또는 채널 ID가 함께 오면 process 값과 같은지 확인한다. `processdata`가 비어 있는 Rich Message 상호작용은 `UserSelection` 또는 `SendBtn` 값을 사용한다. CUBE hello 제어 이벤트는 GAIA와 CUBE 발송을 호출하지 않는다.

## 3. 세션

세션 key는 `사용자 ID + CUBE 채널 ID`다.

```text
(user_id, channel_id) → GAIA session_id
```

최초 요청에는 서버가 임시 session ID를 만든다. GAIA 응답에 session ID가 있으면 이후 요청부터 그 값을 재사용한다. 저장 위치는 프로세스 메모리뿐이므로 앱 재시작 시 사라진다. 질문/답변 기록이나 최근 대화 조회 API는 현재 구현 범위가 아니다.

## 4. GAIA 호출

`.env`의 `GAIA_API_URL`은 완성된 호출 URL이다. 서버는 주소를 붙이거나 바꾸지 않는다.

```text
GAIA_API_URL=http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
```

```http
POST <GAIA_API_URL>
Content-Type: application/json
X-Gaia-Auth-Key: <GAIA_AUTH_KEY>
X-Gaia-User-Id: <CUBE 사용자 ID>
```

```json
{
  "input_value": "CUBE에서 받은 질문",
  "user_id": "CUBE 사용자 ID",
  "session_id": "현재 session ID"
}
```

GAIA 응답은 마지막 Chat Output을 찾는다. 답변은 아래 경로를 우선 사용한다.

```text
results.gaia_response.data.answer
```

값이 없으면 같은 Chat Output의 `results.message.data.text`를 보조값으로 사용한다. 오래된 출력의 답변을 대신 재사용하지 않는다.

## 5. CUBE 답변 발송

정상 답변 또는 오류 안내문을 다음 CUBE API로 보낸다.

```text
CUBE_SEND_URL=http://cube.skhynix.com:8888/legacy/richnotification
```

발송 대상은 callback의 사용자와 채널이다. Rich Notification 본문의 답변은 `content[0].body...control.text[0]`에 넣는다. 현재 확인된 발송 형식에 맞춰 `content[0].process`도 빈 객체로 보내지 않는다.

GAIA 호출이나 답변 추출이 실패하면 `USER_ERROR_MESSAGE`를 같은 CUBE 대상에게 한 번 보내려고 시도하고, callback 요청에는 오류 상태를 반환한다. CUBE 발송 자체의 timeout/중복 발송 정책은 아직 확정되지 않아 자동 재시도를 하지 않는다.

## 6. HTTP 경로

| 방식 | 경로 | 용도 |
| --- | --- | --- |
| `GET` | `/` | FastAPI 문서 화면으로 이동 |
| `GET` | `/hello` | 단순 기동 확인 |
| `GET` | `/health` | HCP 앱의 기동 상태 확인 |
| `POST` | `/api/v1/receiver` | CUBE callback 및 수동 전체 흐름 시험 |

HCP 컨테이너의 Uvicorn 실행 설정은 아래처럼 고정한다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

## 7. 운영 전 확인 항목

- CUBE callback 인증/서명 또는 송신 IP 검증 방식
- CUBE callback의 timeout, 재전송, 중복 event 규칙
- 서비스 → CUBE 및 CUBE → HCP 방화벽 허용 규칙
- CUBE 메시지 발송 성공/오류 body와 최대 메시지 길이
- CUBE 사용자 ID가 GAIA 권한 사번으로 사용 가능한지

상세 실행 절차는 [production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md](production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)를 따른다.
