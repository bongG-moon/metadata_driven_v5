# 실제 GAIA-CUBE 서버 실행 및 CUBE 연동 가이드

이 문서는 `production_callback_server`를 실제 GAIA와 CUBE에 연결해 시험하는 절차다. 더미 서버와 달리 이 서버는 설정이 완료된 뒤 질문을 받으면 **실제 GAIA API와 실제 CUBE Rich Notification API를 호출한다.**

처음에는 반드시 본인 또는 허가받은 테스트 CUBE 채널과 테스트용 GAIA Agent로 시험한다. 실제 키, 봇 토큰, 사용자 사번은 문서·Git·채팅·로그에 남기지 않는다.

## 1. 이 서버가 실제로 하는 일

```text
사용자가 CUBE 채널에 질문
  → CUBE가 우리 서버의 POST /api/qna 주소로 callback JSON 전송
  → 서버가 사용자 ID, 채널 ID, 질문을 읽음
  → 채널에 고정된 GAIA Agent(svc_id)를 선택
  → GAIA external API 호출
  → GAIA 응답의 마지막 유효 Chat Output에서 답변만 추출
  → CUBE Rich Notification API로 답변 발송
  → CUBE 채팅창에 답변 표시
```

서버가 CUBE에 반환하는 callback 응답과 사용자가 CUBE 채팅창에서 보는 답변은 다르다.

| 구분 | 누가 받는가 | 현재 서버의 예 |
| --- | --- | --- |
| callback 응답 | CUBE 시스템 | `{ "status": "success", "message": "GAIA answer was sent to CUBE." }` |
| Rich Notification 발송 | 원래 질문자와 채널 | GAIA에서 추출한 최종 답변 텍스트 |

## 2. 내일 준비해야 할 값

아래 표에서 **실제 값**을 확보해야 한다. 값은 `.env` 파일에만 넣는다.

| 설정 이름 | 무엇을 넣는가 | 어디에서 받는가 | 비밀값인가 |
| --- | --- | --- | --- |
| `GAIA_BASE_URL` | GAIA API의 기본 주소 | 제공된 GAIA 가이드 또는 GAIA 담당자 | 아니오 |
| `GAIA_AUTH_KEY` | GAIA에서 발급한 인증 키 | GAIA 담당자 | 예 |
| `GAIA_SERVICE_ID` | 호출할 GAIA Agent의 `svc_id` | GAIA Agent 설정 | 보통 아니오 |
| `CUBE_SEND_URL` | CUBE Rich Notification 발송 API의 정확한 주소 | CUBE 담당자 | 아니오 |
| `CUBE_BOT_ID` | 메시지를 보내는 CUBE 봇 ID | CUBE 담당자 | 내부값 |
| `CUBE_BOT_TOKEN` | CUBE 봇 인증 토큰 | CUBE 담당자 | 예 |
| `CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON` | 채널별 GAIA Agent 매핑 | 직접 결정 | 아니오 |

추가로 CUBE 담당자에게 아래 항목을 확인해야 한다. 현재 제공된 코드와 문서만으로는 확정할 수 없다.

- CUBE에 등록할 callback URL의 설정 위치와 등록 권한
- callback 인증/서명 또는 허용 송신 IP 정책
- callback timeout, 재전송, message/event ID 정책
- callback 응답의 공식 `status` 값 (`success`, `ignored` 등이 맞는지)
- 실제 테스트/운영용 `CUBE_SEND_URL`과 네트워크 접근 허용 여부
- CUBE `userId` 또는 `header.from.uniquename`이 GAIA 권한 확인에 사용할 사번과 같은 값인지

제공된 CUBE 발송 주소는 개발 서버 예시다. `.env.example`에 들어 있는 값을 그대로 운영값이라고 가정하지 말고, 내일 사용할 테스트 환경의 주소인지 CUBE 담당자에게 확인한다.

## 3. 주소를 세 가지로 구분하기

주소가 헷갈리지 않도록 아래 세 가지를 분리해서 생각한다.

| 이름 | 예시 | 누가 호출하는가 | 설정 위치 |
| --- | --- | --- | --- |
| 우리 서버의 listen 주소 | `0.0.0.0:8000` | 없음. 서버가 대기하는 방식 | Uvicorn 실행 명령 |
| CUBE가 호출할 callback URL | `http://<사내-서버이름>:8000/api/qna` | CUBE → 우리 서버 | CUBE 봇/채널 설정 |
| CUBE가 답변을 받는 발송 URL | `http://<CUBE-서버>/legacy/richnotification` | 우리 서버 → CUBE | `.env`의 `CUBE_SEND_URL` |

`0.0.0.0`은 모든 네트워크 인터페이스에서 서버가 듣도록 하는 listen 주소일 뿐이다. CUBE에 `http://0.0.0.0:8000/api/qna`를 등록하면 안 된다.

현재 코드의 서버 endpoint는 아래와 같이 고정되어 있다.

| HTTP 방식 | 주소 | 용도 |
| --- | --- | --- |
| `GET` | `/health` | 서버가 기동했고 설정을 읽었는지 확인 |
| `POST` | `/api/qna` | CUBE callback을 받는 실제 주소 |
| `GET` | `/docs` | FastAPI API 문서 화면 |

따라서 CUBE에 등록할 주소 형식은 다음이다.

```text
http://<CUBE에서-접근-가능한-사내-DNS-이름-또는-IP>:8000/api/qna
```

코드 자체는 HTTP로 동작한다. HTTPS가 필요하다면 사내 운영 기준에 따라 승인된 reverse proxy 또는 배포 플랫폼에서 TLS를 종료해야 하며, 임의의 HTTPS URL을 등록해서는 안 된다.

## 4. 서버를 설치할 PC 또는 사내 서버에서 준비하기

아래 명령은 서버를 실행할 Windows PC에서 PowerShell로 실행한다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\production_callback_server
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

마지막 `notepad .env`가 열리면 다음 절에서 설명하는 실제 값을 입력하고 저장한다.

`.env` 파일이 이미 있다면 `Copy-Item`으로 덮어쓰지 않는다. 기존 `.env`의 값이 맞는지 확인한 뒤 필요한 값만 수정한다.

## 5. `.env`에 어떤 값을 넣는가

### 가장 간단한 경우: 테스트 채널 하나 → GAIA Agent 하나

아래는 형태를 보여 주는 예시다. `<...>` 부분은 실제 값으로 바꿔야 하며, 그대로 저장하면 안 된다.

```dotenv
# GAIA
GAIA_BASE_URL=http://gaia.example.internal
GAIA_AUTH_KEY=<GAIA에서-발급받은-인증키>
GAIA_SERVICE_ID=<테스트할-GAIA-Agent-svc_id>
GAIA_TIMEOUT_SECONDS=10

# CUBE 발송
CUBE_SEND_URL=<CUBE-담당자가-확인한-Rich-Notification-발송-URL>
CUBE_BOT_ID=<CUBE-봇-ID>
CUBE_BOT_TOKEN=<CUBE-봇-토큰>
CUBE_TIMEOUT_SECONDS=20

# CUBE callback 응답값: CUBE 공식 가이드에서 다른 값이 확인되면 그때만 변경
CUBE_CALLBACK_SUCCESS_STATUS=success
CUBE_CALLBACK_IGNORED_STATUS=ignored

# GAIA 실패 또는 최종 답변 추출 실패 때 사용자에게 한 번 보낼 안전한 문구
USER_ERROR_MESSAGE=요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.
```

이 경우 `CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON`은 비워 두거나 주석 상태로 둔다. 어떤 CUBE 채널에서 질문하더라도 `GAIA_SERVICE_ID`의 Agent로 간다.

### 여러 CUBE 채널을 서로 다른 GAIA Agent에 연결하는 경우

채널 하나가 Agent 하나에 고정되는 현재 정책을 여러 채널에 적용하려면 아래처럼 한 줄 JSON을 넣는다.

```dotenv
CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON={"TEST_CHANNEL_A":"GAIA_SERVICE_ID_A","TEST_CHANNEL_B":"GAIA_SERVICE_ID_B"}
```

매핑의 동작은 아래와 같다.

| callback의 채널 ID | `GAIA_SERVICE_ID` | 채널 매핑 | 실제 선택 Agent |
| --- | --- | --- | --- |
| `TEST_CHANNEL_A` | 있음 | `TEST_CHANNEL_A` 있음 | 매핑의 `GAIA_SERVICE_ID_A` |
| 매핑에 없는 채널 | 있음 | 없음 | 기본 `GAIA_SERVICE_ID` |
| 매핑에 없는 채널 | 비어 있음 | 없음 | 400 오류, GAIA 호출 안 함 |

알 수 없는 채널을 기본 Agent로 보내고 싶지 않다면 `GAIA_SERVICE_ID=`처럼 빈 값으로 두고, 테스트할 모든 채널을 JSON 매핑에 넣는다.

`GAIA_SERVICE_ID=PASTE_DEFAULT_GAIA_SERVICE_ID_HERE`처럼 예제 placeholder를 남겨 두면 안 된다. 실제 `svc_id`로 바꾸거나 엄격한 채널 매핑을 쓸 경우 값 전체를 비운다.

### 설정값과 실제 API 요청의 연결

질문이 들어오면 서버는 아래처럼 값을 사용한다.

| 들어온 값 또는 설정 | 서버가 하는 일 |
| --- | --- |
| CUBE 사용자 ID | GAIA 요청 header의 `X-Gaia-User-Id`와 body의 `user_id`에 같은 값으로 넣음 |
| CUBE 채널 ID | GAIA Agent 매핑 선택 및 CUBE 답변의 `channelid[0]`에 사용 |
| CUBE `processdata` | GAIA body의 `input_value`에 사용 |
| 서버가 만든 `gc_<UUID>` 또는 GAIA가 반환한 값 | GAIA body의 `session_id`에 사용 |
| `GAIA_SERVICE_ID` 또는 채널 매핑 값 | `POST /v2/agents/{svc_id}/external`의 `{svc_id}`에 사용 |
| GAIA 최종 답변 | CUBE Rich Notification의 `control.text[0]`에 사용 |

중요한 점은 `GAIA_USER_ID` 같은 별도 설정값이 없다는 것이다. 현재 구현은 callback을 보낸 CUBE 사용자의 ID를 그대로 GAIA 권한 header와 body에 넣는다. 따라서 CUBE의 사용자 ID가 GAIA에서 권한 있는 사번과 다르면 실제 실행은 실패할 수 있다. 이 경우 ID 매핑 규칙을 GAIA/CUBE 담당자에게 먼저 확인한다.

## 6. 실제 호출 없이 설정만 검사하기

아래 검사는 `.env` 형식과 필수값을 읽기만 하며 GAIA나 CUBE에 요청을 보내지 않는다.

```powershell
.\.venv\Scripts\python.exe -c "from app import Settings; s=Settings.from_env(); print('Configuration parsed successfully.'); print('default_agent_configured=', bool(s.default_gaia_service_id)); print('channel_map_count=', len(s.channel_service_map))"
```

성공하면 다음처럼 설정이 읽혔다는 결과만 나온다.

```text
Configuration parsed successfully.
default_agent_configured= True
channel_map_count= 0
```

오류가 나오면 서버를 시작하거나 실제 callback을 보내지 말고 `.env`를 먼저 고친다. 특히 `GAIA_AUTH_KEY`, `CUBE_SEND_URL`, `CUBE_BOT_ID`, `CUBE_BOT_TOKEN`은 비어 있거나 `PASTE_...` 값이면 시작에 실패한다.

## 7. 먼저 내 PC에서 서버 기동 확인하기

처음에는 다른 시스템이 접근하지 못하게 localhost에서 기동한다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

이 PowerShell 창은 서버가 실행 중이므로 열어 둔다. 새 PowerShell 창에서 다음을 실행한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

다음처럼 나오면 서버가 `.env`를 읽고 정상 기동한 것이다.

```text
status mode       configured_default_agent
------ ----       ------------------------
ok     production yes
```

이 단계의 `/health` 호출은 GAIA나 CUBE를 호출하지 않는다.

기동을 마쳤으면 서버 실행 창에서 `Ctrl+C`로 종료한다.

## 8. CUBE가 접근할 수 있게 서버 실행하기

CUBE 연동 시험 직전에 서버를 아래처럼 다시 시작한다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

이 명령은 서버 PC의 네트워크 인터페이스에서 8000 포트를 듣게 한다. CUBE에 등록할 실제 주소를 찾을 때는 사내에서 승인된 DNS 이름을 우선 사용한다.

```powershell
hostname
ipconfig
```

`hostname` 결과 또는 사내 운영자가 알려 준 내부 DNS 이름을 사용해, 예를 들어 다음과 같은 callback URL을 만든다.

```text
http://MY-SERVER-NAME:8000/api/qna
```

같은 PC에서 `127.0.0.1`로 health가 된다고 CUBE 서버에서도 접속되는 것은 아니다. CUBE와 같은 사내망의 다른 PC에서 아래처럼 포트 접근을 확인한다.

```powershell
Test-NetConnection -ComputerName MY-SERVER-NAME -Port 8000
```

`TcpTestSucceeded : True`가 나와야 한다. 실패하면 서버 주소, Windows 방화벽, 사내망 방화벽 또는 CUBE 측 접근 허용을 확인한다. 다른 프로그램을 임의로 종료하거나 포트를 강제로 열지 않는다.

## 9. CUBE에 callback URL을 등록하기

CUBE의 봇 또는 채널 설정 화면에서 callback URL을 등록할 때 아래 값을 사용한다.

```text
HTTP method: POST
Callback URL: http://MY-SERVER-NAME:8000/api/qna
Content-Type: application/json
```

제공된 문서에는 CUBE 관리 화면의 정확한 메뉴와 callback 인증 설정 방법이 없다. 따라서 CUBE 담당자에게 등록을 요청하거나, 제공받은 봇 설정 절차에서 `callbackaddress` 또는 callback URL 항목을 찾는다.

일반 텍스트 질문을 먼저 시험한다. 현재 운영 서버는 Rich Message의 동적 선택값 중 `UserSelection`과 `SendBtn`만 처리한다. 다른 `processid`를 사용하는 버튼/라디오는 실제 계약에 맞춰 코드 확장이 끝난 뒤 시험한다.

## 10. CUBE 등록 전에 서버 → GAIA → CUBE를 한 번 시험하기

아래 테스트는 CUBE가 보내는 callback을 PowerShell로 흉내 낸다. **실제 GAIA와 실제 CUBE 발송 API를 호출하므로**, 본인 또는 허가받은 테스트 사용자·테스트 채널 값만 넣는다.

서버가 8단계의 PowerShell 창에서 실행 중인 상태에서, 새 PowerShell 창을 열어 다음을 실행한다.

```powershell
$callback = @{
  richnotificationmessage = @{
    process = @{
      processdata = "GAIA-CUBE 실제 연동 테스트입니다. 한 줄로 인사해 주세요."
      userId = "<GAIA-권한이-있는-테스트-사번>"
      channelId = "<허가받은-CUBE-테스트-채널-ID>"
    }
  }
}

$body = $callback | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/qna" `
  -ContentType "application/json" `
  -Body $body
```

성공하면 PowerShell에는 다음과 비슷한 callback 처리 결과가 나온다.

```text
status  : success
message : GAIA answer was sent to CUBE.
```

그리고 지정한 테스트 CUBE 채널에는 GAIA Agent의 실제 답변이 나타나야 한다. PowerShell의 `message`는 사용자에게 보일 Agent 답변 자체가 아니다.

위 수동 요청에는 `header`를 넣지 않았는데, 이는 제공된 일반 텍스트 callback 예시에 맞춘 것이다. CUBE가 실제로 `header`와 `process` 값을 모두 보내는 경우 서버는 둘을 비교하며, 사용자 또는 채널 값이 다르면 HTTP 400으로 요청을 거부한다.

## 11. 실제 CUBE 채팅에서 시험하기

callback URL 등록과 10단계의 수동 시험이 성공한 뒤에만 진행한다.

1. 서버 PowerShell 창이 계속 실행 중인지 확인한다.
2. 등록한 CUBE 테스트 채널에서 일반 텍스트 질문을 보낸다.
3. 서버 PowerShell 창에 `POST /api/qna` 요청이 보이는지 확인한다.
4. 같은 CUBE 채널에서 봇의 답변이 오는지 확인한다.
5. 서버를 종료하지 않은 상태에서 이어지는 질문을 한 번 더 보낸다.

두 번째 질문은 첫 답변을 알아야 답할 수 있는 내용으로 보내면 세션 동작을 쉽게 확인할 수 있다. 예를 들면 첫 질문 뒤에 “방금 답변을 한 문장으로 다시 요약해줘”라고 보낸다.

현재 구현은 같은 `사용자 ID + CUBE 채널 ID`에 대해 실행 중인 서버 메모리 안에서 GAIA session ID를 재사용한다. 첫 요청에는 `gc_<UUID>`를 만들고, GAIA가 루트 `session_id`를 반환하면 이후 요청에는 그 반환값을 사용한다. 서버를 재시작하면 새 session ID가 만들어진다. 운영 서버에는 현재 세션 목록 또는 과거 대화를 조회하는 endpoint가 없고, 영구 저장도 하지 않는다.

## 12. 성공·실패 때 무엇을 보면 되는가

| 상황 | callback HTTP 결과 | CUBE 채팅창 | 먼저 확인할 것 |
| --- | --- | --- | --- |
| 정상 | 200, `status=success` | GAIA 답변 표시 | 정상 |
| 최초 진입 sentinel | 200, `status=ignored` | 답변 없음 | `!@#HelloChatBot#@!`는 제어 메시지라 정상 |
| callback JSON 오류 또는 ID 불일치 | 400 | 답변 없음 | CUBE payload의 `header`/`process` 값 |
| GAIA 호출 또는 최종 답변 추출 실패 | 502 | `USER_ERROR_MESSAGE`가 보일 수 있음 | GAIA 키, `svc_id`, 사용자 권한, GAIA 네트워크 |
| CUBE 답변 발송 실패 | 502 | 답변 없음 | `CUBE_SEND_URL`, 봇 ID/토큰, CUBE 네트워크 |

서버는 실제 키와 전체 사용자 질문을 로그에 남기지 않는다. 터미널의 오류 분류를 확인하되, 오류 메시지나 `.env` 내용을 다른 곳에 복사하지 않는다.

## 13. 내일 시험 체크리스트

- [ ] 테스트할 CUBE 채널 ID를 받았다.
- [ ] 테스트할 GAIA Agent의 `svc_id`와 `GAIA_AUTH_KEY`를 받았다.
- [ ] CUBE 봇 ID·봇 토큰·실제 `CUBE_SEND_URL`을 받았다.
- [ ] CUBE 사용자의 ID가 GAIA 권한용 사번과 같은지 확인했다.
- [ ] `.env`에 실제 값을 입력했고 Git에 추가하지 않았다.
- [ ] 설정 파싱 검사와 `GET /health`가 성공했다.
- [ ] 서버에서 GAIA와 CUBE 발송 URL로 나가는 사내망 연결이 허용된다.
- [ ] CUBE가 `http://<서버이름>:8000/api/qna`에 들어올 수 있다.
- [ ] 먼저 PowerShell 수동 callback 테스트를 테스트 채널에서 성공했다.
- [ ] 그 뒤 CUBE 채팅에서 일반 텍스트 질문을 성공했다.

## 14. 현재 구현의 범위와 주의점

이 서버는 내일의 기본 연동 시험을 위한 동기형 구현이다. 다음 기능은 아직 포함하지 않는다.

- callback 서명/인증 검증
- 메시지 중복 방지와 재전송 처리
- MongoDB 같은 영구 세션·대화 기록 저장
- 세션 또는 대화 내용 조회 API
- worker, outbox, 재시도 큐, 스케줄러
- `UserSelection`, `SendBtn` 이외의 동적 Rich Message 선택값 처리

따라서 callback 인증 규칙과 timeout·재전송 정책을 확인하기 전에는 이 서버를 인터넷에 공개하지 않는다. 사내 테스트 채널에서 기본 연결을 검증한 뒤, 운영 정책이 확정되면 그 계약에 맞춰 보완한다.

## 참고 문서

- [운영 서버 README](README.md)
- [GAIA 외부 Langflow 실행 API](../base_guide/gaia/01_external_langflow_api.md)
- [GAIA 응답 최종 답변 추출 규칙](../base_guide/gaia/02_response_extraction.md)
- [CUBE callback 계약](../base_guide/cube/02_callback_api.md)
- [CUBE Rich Notification 발송 계약](../base_guide/cube/01_message_send_api.md)
