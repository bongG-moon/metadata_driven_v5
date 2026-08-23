# 운영용 GAIA-CUBE Callback Server

이 폴더는 현재 합의된 **기본 동기 흐름**만 실행한다.

처음 실제 연동을 시험한다면 [PRODUCTION_SERVER_RUN_GUIDE.md](PRODUCTION_SERVER_RUN_GUIDE.md)의 단계별 실행 가이드를 먼저 따른다.

```text
CUBE callback 수신
→ 사용자·채널·질문 추출
→ 채널에 고정된 GAIA Agent 선택
→ 같은 사용자·채널의 GAIA session_id 사용
→ GAIA API 호출 및 최종 Chat Output 답변 추출
→ CUBE Rich Notification 발송
→ callback 처리 결과 반환
```

MongoDB, worker, outbox, 재시도 큐, 스케줄러는 포함하지 않는다. 세션 매핑은 프로세스 메모리에만 있으므로 서버를 재시작하면 새 GAIA `session_id`가 만들어진다.

## 준비

PowerShell에서 이 폴더로 이동한 뒤 실행한다.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`를 열어 다음 실제 값을 입력한다.

- `GAIA_AUTH_KEY`: GAIA에서 발급받은 인증 키
- `GAIA_SERVICE_ID`: 기본으로 연결할 GAIA Agent의 `svc_id`
- `CUBE_SEND_URL`: 실제 사용할 CUBE 발송 API 주소
- `CUBE_BOT_ID`: CUBE 봇 ID
- `CUBE_BOT_TOKEN`: CUBE 봇 토큰

채널마다 다른 GAIA Agent를 쓸 때만 `CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON`에 채널 ID와 `svc_id` 매핑을 입력한다. 채널 매핑이 있으면 그 값이 기본 `GAIA_SERVICE_ID`보다 우선한다.

## 실행

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

`0.0.0.0`은 서버가 모든 네트워크 인터페이스에서 듣게 하는 **listen 주소**일 뿐, CUBE에 등록할 주소가 아니다. CUBE에는 실제 사내 접근 가능한 서버 이름 또는 IP를 사용한 주소를 등록한다.

```text
http://<서버-이름-또는-내부-IP>:8000/api/qna
```

정상 기동 확인 주소는 다음과 같다.

```text
http://<서버-이름-또는-내부-IP>:8000/health
```

## CUBE 채널과 GAIA Agent의 연결

현재 정책은 CUBE 채널 하나가 GAIA Agent 하나에 고정되는 방식이다. callback payload가 `svc_id`를 정하지 않는다.

```text
CUBE channel ID
  → .env의 채널-대-GAIA 매핑 또는 기본 GAIA_SERVICE_ID
  → GAIA external Agent API
```

세션은 `사용자 + CUBE 채널`을 기준으로 구분한다. 같은 그룹 채널에 여러 사람이 있어도 각 사용자의 GAIA 대화 문맥이 섞이지 않는다.

## 실제 요청 처리

`POST /api/qna`는 제공된 CUBE callback 구조에서 다음 값을 읽는다.

| 값 | 우선 경로 | 보조 경로 |
| --- | --- | --- |
| 사용자 | `header.from.uniquename` | `process.userId` |
| 채널 | `header.to.channelid[0]` | `process.channelId` |
| 질문 | `process.processdata` | `UserSelection` 또는 `SendBtn` |

header와 process에 사용자·채널 값이 모두 있으면 값이 같아야 한다. `!@#HelloChatBot#@!`는 제어 메시지이므로 GAIA와 CUBE 발송을 호출하지 않고 `ignored` 상태를 반환한다.

GAIA 응답에서는 마지막 `Chat Output`만 찾고, 우선 `results.gaia_response.data.answer`를 CUBE로 보낸다. 답변이 없거나 GAIA 호출이 실패하면 `.env`의 `USER_ERROR_MESSAGE`를 CUBE에 한 번 보낸 뒤 callback에는 안전한 오류 상태를 반환한다.

## 테스트와 주의사항

이 폴더의 운영용 서버는 실제 URL과 키가 있어야 한다. 외부 API를 호출하지 않는 테스트는 상위의 `dummy_callback_server/`를 사용한다.

운영용 코드 자체의 callback → GAIA → CUBE payload 흐름은 실제 네트워크 없이 아래 테스트로 확인할 수 있다.

```powershell
.\.venv\Scripts\python.exe -m pytest test_app.py -q
```

현재 제공된 자료에는 callback 인증/서명, 공식 callback `status`, timeout, 재전송 및 event ID 정책이 없다. 따라서 CUBE 담당 가이드가 확보되기 전에는 인터넷에 이 서버를 공개하지 말고, 사내 테스트 채널에서만 사용한다. 실제 인증 규칙을 받으면 callback 인증 검증을 추가해야 한다.

`.env`에는 실제 인증 키와 토큰이 들어가므로 절대 Git에 추가하거나 로그에 출력하지 않는다.
