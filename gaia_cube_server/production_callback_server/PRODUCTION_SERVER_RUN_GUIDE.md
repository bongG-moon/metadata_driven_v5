# HCP에서 GAIA-CUBE 서버 실행하기

이 안내는 CUBE 메시지 한 건이 GAIA를 실행하고, 생성된 답변이 다시 CUBE로 발송되는 전체 흐름을 시험하는 방법이다. 별도의 로컬/운영 모드 선택은 없다. 이 서버는 등록된 HCP callback URL 하나를 사용한다.

```text
CUBE 또는 수동 callback 요청
  → HCP 서버의 POST /api/v1/receiver
  → GAIA 호출
  → CUBE 답변 발송
```

등록된 URL은 다음과 같다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

## 1. 준비할 값

아래 값은 실제 값으로 바꿔 HCP Secret/환경변수 또는 서버 폴더의 `.env`에 입력한다. 비밀값을 채팅, 문서, Git에 적지 않는다.

| 환경변수 | 입력할 값 |
| --- | --- |
| `GAIA_API_URL` | Agent ID까지 포함한 **완성된** GAIA API URL |
| `GAIA_AUTH_KEY` | GAIA 인증 키 |
| `GAIA_TIMEOUT_SECONDS` | GAIA 응답 대기 시간(초), 기본 10 |
| `CUBE_SEND_URL` | CUBE Rich Notification 발송 전체 URL |
| `CUBE_BOT_ID` | CUBE 봇 ID |
| `CUBE_BOT_TOKEN` | CUBE 봇 토큰 |
| `CUBE_BOT_FROMUSERNAME_JSON` | 봇 표시 이름 5개 JSON 배열 |
| `CUBE_TIMEOUT_SECONDS` | CUBE 발송 응답 대기 시간(초), 기본 20 |
| `USER_ERROR_MESSAGE` | GAIA 처리 실패 시 원인 안내 뒤에 붙일 재시도 안내문 |

HCP에 배포할 프로젝트 폴더에서 `.env.example`을 `.env`로 복사할 때는 다음과 같이 한다.

```powershell
Copy-Item .env.example .env
notepad .env
```

핵심은 `GAIA_API_URL`에 기본 주소가 아니라 **바로 호출 가능한 전체 주소**를 넣는 것이다.

```dotenv
GAIA_API_URL=http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
GAIA_AUTH_KEY=<GAIA_AUTH_KEY>

CUBE_SEND_URL=http://cube.skhynix.com:8888/legacy/richnotification
CUBE_BOT_ID=<CUBE_BOT_ID>
CUBE_BOT_TOKEN=<CUBE_BOT_TOKEN>
CUBE_BOT_FROMUSERNAME_JSON=["<한글 이름>","<일본어 이름>","<영어 이름>","<중문 이름>","<기타 이름>"]
```

`GAIA_API_URL` 안에 Agent 식별자가 이미 들어 있으므로 서버는 다른 주소 조합이나 채널별 Agent 선택을 하지 않는다.

## 2. HCP에 올릴 때 확인할 것

HCP의 배포 설정에서 다음 두 항목을 확인한다.

1. HCP 서비스가 위 callback URL의 `/api/v1/receiver` 요청을 이 앱으로 전달하는지
2. HCP ingress의 backend/target port가 앱의 고정 포트 `5000`인지

앱의 고정 실행 코드는 바꾸지 않는다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

HCP 실행 명령은 다음과 같다.

```powershell
python app.py
```

HCP의 실행 환경에서 `python` 명령을 지정하는 방식은 배포 화면의 기준을 따른다. 로컬에서 이 명령을 실행해도 HCP URL이 자동으로 생성되지는 않는다.

## 3. 서버가 켜졌는지 확인하기

브라우저 또는 PowerShell에서 다음 주소를 호출한다.

```powershell
Invoke-RestMethod "http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/health"
```

정상이라면 다음과 비슷한 JSON을 받는다.

```json
{
  "status": "ok",
  "callback_path": "/api/v1/receiver"
}
```

이 확인은 GAIA나 CUBE에 메시지를 보내지 않는다.

## 4. 내가 질문을 직접 입력해 GAIA와 CUBE 발송을 시험하기

CUBE callback을 흉내 내지 않고, 개발자가 질문·수신자·채널을 코드에 직접 입력해 전체 발송을 시험할 수 있다. `manual_gaia_cube_send.py`는 HTTP API가 아니며, 외부에 공개되는 `/send` 주소도 만들지 않는다.

HCP의 실행 환경에서 `production_callback_server/manual_gaia_cube_send.py`를 연다. 파일 맨 위에서 아래 다섯 값을 확인하고 실제 허가된 시험 값으로 바꾼다.

```python
MESSAGE = "GAIA-CUBE 실제 연동 테스트입니다. 한 줄로 인사해 주세요."
RECEIVER_ID = "AUTHORIZED_EMPLOYEE_ID"
# 사번으로만 발송할 때는 비워 둔다.
CHANNEL_ID = ""

# GAIA 권한 사번이 수신자와 다를 때만 입력한다.
GAIA_USER_ID = ""

# 빈 문자열이면 새 대화를 시작한다.
SESSION_ID = ""
```

`MESSAGE`와 `RECEIVER_ID`는 반드시 입력한다. `CHANNEL_ID`는 선택 사항이며, 사번으로만 발송할 때는 비워 둔다. `GAIA_USER_ID`를 비워 두면 `RECEIVER_ID`를 GAIA 사용자 ID로 사용한다. `SESSION_ID`를 비워 두면 매번 새 GAIA 대화를 시작한다. 이 도구는 `app.py`와 같은 `.env`/환경변수 설정을 사용하며, 키와 토큰은 이 파일이 아니라 `.env` 또는 HCP Secret/환경변수에만 둔다.

값을 저장한 뒤 HCP 실행 환경의 `production_callback_server` 폴더에서 아래 한 줄을 실행한다.

```powershell
python manual_gaia_cube_send.py
```

이 명령은 현재 폴더의 `.env` 또는 HCP 환경변수에서 `GAIA_API_URL`, `GAIA_AUTH_KEY`, CUBE 봇 설정을 읽어 다음 순서로 **실제 외부 호출**을 수행한다.

```text
직접 입력한 질문
  → GAIA_API_URL 호출
  → GAIA 최종 답변 추출
  → CUBE Rich Notification 발송
  → 지정한 수신자 사번의 CUBE에서 답변 확인
```

실행 결과는 GAIA session ID와 답변을 PowerShell에 출력한다. 그래도 실제 발송 성공은 수신자 사번의 CUBE에 답변이 도착했는지까지 확인해야 한다. 이 시험은 실제 메시지를 전송하므로, 본인 또는 허가된 테스트 수신자만 사용한다.

## 5. CUBE가 등록 URL을 호출할 때의 처리 순서

CUBE callback에는 사용자, 채널, 질문이 들어 있다. 서버는 아래 값을 읽는다.

| 필요한 정보 | CUBE callback 위치 |
| --- | --- |
| 사용자 ID | `richnotificationmessage.process.userId` 또는 `header.from.uniquename` |
| 채널 ID | `richnotificationmessage.process.channelId` 또는 `header.to.channelid[0]` |
| 질문 | `richnotificationmessage.process.processdata` |

header와 process에 같은 ID가 모두 들어 있으면 둘은 반드시 일치해야 한다. 불일치하면 잘못된 요청으로 처리하고 GAIA에 보내지 않는다.

정상 요청의 내부 흐름은 다음과 같다.

1. `사용자 ID + 채널 ID`로 현재 GAIA session ID를 찾거나 새로 만든다.
2. GAIA에 아래 형식으로 요청한다.

   ```json
   {
     "input_value": "CUBE에서 받은 질문",
     "user_id": "CUBE 사용자 ID",
     "session_id": "현재 세션 ID"
   }
   ```

3. GAIA 응답의 마지막 Chat Output에서 `results.gaia_response.data.answer`를 우선 읽는다.
4. 추출한 답변을 CUBE Rich Notification API로 보낸다. 이 payload의 `content[0].process`는 비어 있지 않게 구성된다.
5. callback을 보낸 쪽에는 처리 결과 JSON을 돌려준다.

같은 사용자와 같은 채널의 다음 질문은 GAIA가 돌려준 session ID를 재사용한다. 단, 이 정보는 메모리에만 있으므로 HCP 앱 재시작 뒤에는 새 대화가 시작된다.

## 6. callback 형식 전체 흐름 시험 (선택)

아직 CUBE 등록을 기다리는 동안에도, PowerShell에서 CUBE callback과 같은 형태의 JSON을 HCP URL로 보내 전체 흐름을 시험할 수 있다. 이 요청은 **실제 GAIA를 호출하고 실제 CUBE 채널에 답변을 보낸다.** 반드시 본인 또는 허가받은 테스트 사용자와 채널을 사용한다. 다만 직접 질문을 입력해 발송만 확인하려면 앞 절의 `manual_gaia_cube_send.py`가 더 단순하다.

새 PowerShell 창에서 아래를 실행한다. `AUTHORIZED_EMPLOYEE_ID`와 `APPROVED_CUBE_TEST_CHANNEL_ID`만 실제 승인된 값으로 바꾼다.

```powershell
$callback = @{
  richnotificationmessage = @{
    process = @{
      processdata = "GAIA-CUBE 실제 연동 테스트입니다. 한 줄로 인사해 주세요."
      userId = "AUTHORIZED_EMPLOYEE_ID"
      channelId = "APPROVED_CUBE_TEST_CHANNEL_ID"
    }
  }
}

$body = $callback | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver" `
  -ContentType "application/json" `
  -Body $body
```

성공 시 PowerShell에는 다음과 비슷한 처리 결과가 보인다.

```json
{
  "status": "success",
  "message": "GAIA answer was sent to CUBE."
}
```

이 JSON은 CUBE 시스템에 돌려주는 **처리 결과 확인(ACK)** 이며 GAIA의 답변 본문이 아니다. 현재 최소 구현은 worker 없이 한 요청 안에서 GAIA 호출과 CUBE 발송까지 마친 뒤 이 결과를 반환한다. 실제 답변은 `APPROVED_CUBE_TEST_CHANNEL_ID`의 CUBE 채팅창에 도착해야 한다. 즉, PowerShell 응답과 CUBE 채팅창을 모두 확인한다.

## 7. CUBE 등록 후에는 무엇이 달라지는가

수동 PowerShell 요청 대신 CUBE가 같은 URL로 POST 요청을 보낸다는 점만 다르다. 사용자가 CUBE 채널에 입력하면 자동으로 아래가 실행된다.

```text
사용자 질문 → CUBE callback → HCP 서버 → GAIA → CUBE 답변
```

서버에는 사용자가 직접 호출할 공개 `/send` endpoint가 없다. `manual_gaia_cube_send.py`는 서버 내부에서 사람이 실행하는 시험 도구일 뿐 HTTP endpoint가 아니다.

## 8. 문제를 확인하는 순서

| 증상 | 먼저 확인할 것 |
| --- | --- |
| `/health`가 안 열림 | HCP 서비스 기동 여부와 ingress의 5000 포트 연결 |
| callback이 HCP에 도착하지 않음 | CUBE 등록 URL, CUBE → HCP 방화벽/허용 IP |
| callback은 왔지만 답변이 없음 | `GAIA_API_URL`, GAIA 권한 키, callback 사용자 ID가 GAIA 권한 사번인지 |
| GAIA는 성공했는데 CUBE 답변이 없음 | `CUBE_SEND_URL`, 봇 ID·토큰, 서비스 → CUBE 방화벽 |
| 앱 재시작 뒤 앞 대화를 잊음 | 현재 구현은 session ID를 메모리에만 저장함 |

callback 인증/서명, CUBE 재전송, CUBE 메시지 중복 방지 정책은 제공된 계약에서 확정되지 않았다. 실제 사용자 범위를 넓히기 전에는 CUBE 담당자와 이 정책 및 방화벽 설정을 확인한다.
