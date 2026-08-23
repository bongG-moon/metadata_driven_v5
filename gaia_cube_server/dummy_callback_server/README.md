# 더미 GAIA-CUBE Callback Server

이 폴더는 **외부 CUBE, GAIA API를 전혀 호출하지 않고** 기본 연결 흐름을 로컬에서 확인하는 FastAPI 서버다.

처음 실행한다면 [DUMMY_SERVER_RUN_GUIDE.md](DUMMY_SERVER_RUN_GUIDE.md)의 단계별 PowerShell 가이드를 먼저 따른다.

```text
CUBE callback JSON
  -> 사용자·채널·입력/선택값 확인
  -> 채널에 연결된 더미 GAIA Agent 선택
  -> 사용자 + 채널별 in-memory GAIA session_id 재사용
  -> GAIA 같은 모양의 더미 응답 생성
  -> 마지막 Chat Output에서 최종 답변 추출
  -> CUBE Rich Notification 요청 JSON 생성 및 메모리에 보관
  -> callback 처리 결과 반환
```

실제 네트워크 발송은 하지 않는다. 따라서 CUBE/GAIA 키, 사번, 사내망 연결 없이 실행할 수 있다.

## 실행

PowerShell에서 다음을 실행한다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\dummy_callback_server
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

서버가 시작되면 다음 주소를 사용할 수 있다.

- Swagger UI: `http://127.0.0.1:8001/docs`
- 상태 확인: `http://127.0.0.1:8001/health`
- CUBE callback: `POST http://127.0.0.1:8001/api/qna`

## 바로 해보는 테스트

다른 PowerShell 창에서 아래 요청을 보낸다. header와 process 양쪽에 있는 사용자/채널 ID는 같은 값이어야 한다.

```powershell
$body = @{
  richnotificationmessage = @{
    header = @{
      from = @{ uniquename = "EMPLOYEE_ID_EXAMPLE" }
      to = @{ channelid = @("CHANNEL_ID_EXAMPLE") }
    }
    process = @{
      processdata = "오늘 생산 현황을 알려줘"
      userId = "EMPLOYEE_ID_EXAMPLE"
      channelId = "CHANNEL_ID_EXAMPLE"
    }
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/api/qna" `
  -ContentType "application/json" `
  -Body $body
```

성공하면 callback 응답에 `status: success`가 나온다. 이 응답의 `message`는 CUBE 화면에 표시되는 실제 답변이 아니라 callback 처리 결과다. 제공된 CUBE 예시에 없는 session ID는 callback ACK에 추가하지 않으며, 아래 테스트 조회 endpoint에서 확인한다.

사용자에게 보낼 것으로 만들어진 Rich Notification JSON은 별도 endpoint에서 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/outgoing-messages |
  ConvertTo-Json -Depth 20
```

여기서 다음 경로가 사용자가 보게 될 더미 GAIA 답변이다.

```text
outgoing_messages[0]
  .richnotification.content[0].body.row[0].column[0].control.text[0]
```

## Rich Message 선택값 테스트

`processdata`가 비어 있어도 라디오/버튼 값이 있으면 callback을 처리한다. `UserSelection`, `SendBtn`처럼 CUBE Rich Message의 `processid`가 동적 key로 전달된다는 제공 가이드를 재현한다.

```powershell
$body = @{
  richnotificationmessage = @{
    header = @{
      from = @{ uniquename = "EMPLOYEE_ID_EXAMPLE" }
      to = @{ channelid = @("CHANNEL_ID_EXAMPLE") }
    }
    process = @{
      processdata = ""
      userId = "EMPLOYEE_ID_EXAMPLE"
      channelId = "CHANNEL_ID_EXAMPLE"
      UserSelection = "2"
      SendBtn = "submit"
    }
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/api/qna" `
  -ContentType "application/json" `
  -Body $body
```

더미 GAIA에 전달된 변환 결과와 전체 더미 GAIA 응답은 다음에서 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/gaia-runs |
  ConvertTo-Json -Depth 30
```

## 세션을 확인하는 방법

같은 사용자와 같은 CUBE 채널에서 두 번 요청하면 `/api/test/sessions`의 같은 `gaia_session_id`가 재사용된다. 다른 사용자 또는 다른 채널이면 다른 session ID가 만들어진다. 이 더미 구현은 프로세스 메모리만 사용하므로 서버를 재시작하거나 reset하면 모두 사라진다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/sessions |
  ConvertTo-Json -Depth 10
```

테스트 중 쌓인 더미 세션, GAIA 실행, 발송 요청을 모두 비우려면 아래를 실행한다.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/test/reset
```

## 더미 채널 → GAIA Agent 설정

기본 연결은 아래와 같다.

| CUBE 채널 ID | 더미 GAIA service ID |
| --- | --- |
| `CHANNEL_ID_EXAMPLE` | `DUMMY_GAIA_PRODUCTION_AGENT` |
| `500008005` | `DUMMY_GAIA_PRODUCTION_AGENT` |
| `CHANNEL_QUALITY_EXAMPLE` | `DUMMY_GAIA_QUALITY_AGENT` |

다른 채널을 시험하려면 서버를 시작하기 전에 환경변수로 전체 매핑을 지정한다.

```powershell
$env:DUMMY_CHANNEL_GAIA_MAP = '{"MY_TEST_CHANNEL":"DUMMY_GAIA_MY_AGENT"}'
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

이 설정은 더미 Agent 이름일 뿐 GAIA 서비스나 인증 키를 호출하지 않는다. 현재 적용된 매핑은 `GET /api/test/config`에서 볼 수 있다.

## endpoint 목록

| Endpoint | 용도 |
| --- | --- |
| `POST /api/qna` | CUBE callback 수신 및 전체 더미 흐름 실행 |
| `GET /health` | 더미 서버 상태 확인 |
| `GET /api/test/config` | 더미 채널-Agent 매핑 확인 |
| `GET /api/test/sessions` | 현재 in-memory 세션 확인 |
| `GET /api/test/gaia-runs` | 더미 GAIA 입력, 원본 응답, 추출 결과 확인 |
| `GET /api/test/outgoing-messages` | 실제 CUBE로 보낼 형식의 payload 확인 |
| `POST /api/test/reset` | 테스트 메모리 초기화 |

`/api/test/*` endpoint는 더미 테스트를 위한 것이며 실제 운영 서버에는 포함하지 않는다.

## 자동 테스트

이 폴더에서 아래 명령을 실행하면 callback 파싱, 세션 재사용, Rich Message 선택값, 마지막 Chat Output 추출, CUBE 발송 payload 캡처, 입력 불일치 및 hello handshake 처리를 확인한다.

```powershell
python -m pytest tests -q
```

## 이 서버가 검증하는 것과 검증하지 않는 것

검증하는 것:

- 제공된 CUBE callback의 `header`/`process` key 파싱
- `header.from.uniquename`와 `process.userId`, `header.to.channelid[0]`와 `process.channelId`의 일치 확인
- 채널 하나가 하나의 GAIA Agent에 고정되는 라우팅
- 사용자 + 채널별 GAIA session ID 재사용
- GAIA 응답의 마지막 유효 Chat Output에서 최종 답변을 찾는 규칙
- CUBE Rich Notification text payload의 구조

검증하지 않는 것:

- 실제 CUBE callback 인증, 서명, timeout, 재전송 정책
- 실제 GAIA 인증, 권한, HTTP 오류 또는 session 누적 정책
- 실제 CUBE 발송 성공 여부
- MongoDB, worker, outbox, 재시도, 스케줄러 같은 향후 운영 설계

실제 키를 넣어 외부 API를 호출할 서버는 상위 `gaia_cube_server`의 별도 실제 환경 폴더를 사용한다.
