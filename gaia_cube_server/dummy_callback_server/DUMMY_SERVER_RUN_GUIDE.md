# 더미 Callback 서버 실행 가이드

이 문서는 **실제 CUBE와 GAIA에 연결하지 않고**, 내 PC에서 CUBE → GAIA → CUBE 답변 흐름을 직접 확인하는 방법을 설명한다. API 키, 사번, 사내망 연결은 필요하지 않다.

이 더미 서버가 하는 일은 다음과 같다.

```text
테스트용 CUBE callback 요청
  → 사용자 / 채널 / 질문 확인
  → 채널에 연결된 더미 GAIA Agent 선택
  → 더미 GAIA 답변 생성
  → CUBE에 보낼 Rich Notification JSON 생성
  → 실제 전송 대신 메모리에 보관
```

즉, 이 가이드에서는 CUBE 채팅창에 실제 메시지가 도착하지 않는다. 대신 서버가 **CUBE로 보낼 내용**을 테스트 조회 API에서 확인한다.

## 1. 준비물

- Windows PowerShell
- Python 3 (`py --version` 또는 `python --version`으로 확인)
- 이 프로젝트 폴더

아래 명령은 PowerShell 기준이다. 명령을 복사할 때 `C:\Users\qkekt\Desktop\metadata_driven_v5` 부분이 현재 프로젝트 위치와 다르면 그 부분만 바꾼다.

## 2. 처음 한 번만 설치하기

PowerShell을 열고 아래를 순서대로 실행한다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\dummy_callback_server
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

마지막 명령이 끝나면 FastAPI와 Uvicorn이 이 폴더의 `.venv`에 설치된다.

`Activate.ps1` 실행이 PowerShell 정책 때문에 막히면 가상환경을 활성화하지 않아도 된다. 아래처럼 `.venv` 안의 Python을 직접 사용한다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. 더미 서버 시작하기

설치가 끝난 같은 PowerShell 창에서 아래를 실행한다.

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

가상환경을 활성화하지 않았다면 아래 명령을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

`Application startup complete`와 비슷한 로그가 보이면 시작된 것이다. 이 창은 서버가 실행 중이므로 그대로 열어 둔다.

사용할 주소는 다음과 같다.

| 주소 | 용도 |
| --- | --- |
| `http://127.0.0.1:8001/docs` | 웹에서 API 목록과 요청 형식을 보는 화면 |
| `http://127.0.0.1:8001/health` | 서버가 살아 있는지 확인 |
| `POST http://127.0.0.1:8001/api/qna` | CUBE callback을 흉내 내는 요청 |

## 4. 서버가 켜졌는지 확인하기

새 PowerShell 창을 하나 더 열고 아래를 실행한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

다음과 비슷한 결과가 나오면 정상이다.

```text
status mode
------ ----
ok     dummy
```

이후의 테스트 명령도 모두 두 번째 PowerShell 창에서 실행한다.

## 5. 첫 번째 CUBE 질문 보내기

아래 요청은 사용자가 `EMPLOYEE_ID_EXAMPLE`이고, `CHANNEL_ID_EXAMPLE` 채널에서 “오늘 생산 현황을 알려줘”라고 입력한 상황을 재현한다.

`header`와 `process` 양쪽의 사용자 ID 및 채널 ID는 반드시 같아야 한다. 이는 CUBE가 전달한 두 위치의 정보를 서로 확인하기 위함이다.

```powershell
$callback = @{
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
}

$body = $callback | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/api/qna" `
  -ContentType "application/json" `
  -Body $body
```

다음처럼 나오면 callback 처리는 성공이다.

```text
status  : success
message : Dummy CUBE Rich Notification was captured.
targetUser : EMPLOYEE_ID_EXAMPLE
```

여기서 `message`는 사용자가 보는 Agent 답변이 아니다. 서버가 callback을 정상 처리했고, **CUBE로 보낼 메시지 JSON을 만들어 보관했다**는 뜻이다.

## 6. 사용자가 받을 답변 확인하기

아래 명령으로 서버가 실제 CUBE 발송 대신 저장한 Rich Notification 요청 전체를 확인한다.

```powershell
$outgoing = Invoke-RestMethod http://127.0.0.1:8001/api/test/outgoing-messages
$outgoing | ConvertTo-Json -Depth 20
```

마지막 발송 내용의 답변 텍스트만 바로 보고 싶으면 아래를 실행한다.

```powershell
$outgoing.outgoing_messages[-1].richnotification.content[0].body.row[0].column[0].control.text[0]
```

다음과 비슷한 답변이 나온다.

```text
[더미 GAIA | DUMMY_GAIA_PRODUCTION_AGENT]
'오늘 생산 현황을 알려줘' 요청을 받았습니다. 이 응답은 외부 GAIA를 호출하지 않고 만든 테스트용 최종 답변입니다.
```

이 값이 실제 환경에서는 CUBE Rich Notification API로 전송되어 해당 사용자의 CUBE 채팅창에 표시될 내용이다.

## 7. 같은 대화의 세션이 유지되는지 확인하기

같은 사용자와 같은 채널로 질문을 하나 더 보낸다.

```powershell
$callback["richnotificationmessage"]["process"]["processdata"] = "어제 생산 현황도 알려줘"
$body = $callback | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/api/qna" `
  -ContentType "application/json" `
  -Body $body
```

이제 세션 목록을 조회한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/sessions |
  ConvertTo-Json -Depth 10
```

하나의 `sessions` 항목이 보이며 `request_count`는 `2`가 된다. 그 항목의 `gaia_session_id`가 두 요청에 공통으로 사용된 세션 ID다.

현재 더미 서버의 세션 구분 기준은 다음과 같다.

```text
사용자 ID + CUBE 채널 ID
```

따라서 사용자가 같아도 채널이 다르면 별도 세션이 생긴다. 또한 이 더미 세션은 메모리에만 있으므로 서버를 재시작하면 사라진다.

## 8. Rich Message 선택값도 시험하기 (선택)

CUBE Rich Message의 버튼이나 라디오 선택값은 `process` 아래에 동적인 key로 들어올 수 있다. 아래 예시는 `UserSelection`, `SendBtn` 값을 포함한 요청이다.

```powershell
$richCallback = @{
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
}

$richBody = $richCallback | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/api/qna" `
  -ContentType "application/json" `
  -Body $richBody
```

서버가 GAIA에 넘긴 변환된 질문과 더미 GAIA 원본 응답은 다음에서 볼 수 있다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/gaia-runs |
  ConvertTo-Json -Depth 30
```

## 9. 테스트 기록 초기화하기

아래 명령은 더미 서버가 메모리에 보관한 세션, GAIA 실행 기록, CUBE 발송 payload를 모두 비운다.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/test/reset
```

결과는 다음과 같다.

```text
status
------
reset
```

## 10. 다른 테스트 채널을 사용하고 싶을 때

기본으로 사용할 수 있는 채널과 더미 Agent 연결은 다음과 같다.

| CUBE 채널 ID | 더미 GAIA Agent |
| --- | --- |
| `CHANNEL_ID_EXAMPLE` | `DUMMY_GAIA_PRODUCTION_AGENT` |
| `500008005` | `DUMMY_GAIA_PRODUCTION_AGENT` |
| `CHANNEL_QUALITY_EXAMPLE` | `DUMMY_GAIA_QUALITY_AGENT` |

다른 채널 ID를 시험하려면 먼저 실행 중인 서버를 `Ctrl+C`로 종료한다. 그 다음 서버를 시작할 PowerShell 창에서 아래를 실행한다.

```powershell
$env:DUMMY_CHANNEL_GAIA_MAP = '{"MY_TEST_CHANNEL":"DUMMY_GAIA_MY_AGENT"}'
python -m uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

이 환경변수는 기본 매핑 전체를 바꾼다. 따라서 위 설정 후에는 callback JSON의 `channelid`와 `channelId`를 둘 다 `MY_TEST_CHANNEL`로 바꿔서 요청한다.

현재 적용된 매핑은 아래로 확인할 수 있다.

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/test/config |
  ConvertTo-Json -Depth 5
```

## 자주 발생하는 문제

| 증상 | 확인 방법 / 해결 방법 |
| --- | --- |
| `Connection refused` 또는 연결 실패 | 서버를 실행한 첫 번째 창이 열려 있는지 확인하고, `/health`를 다시 호출한다. |
| `Address already in use` | 8001 포트를 다른 프로그램이 사용 중이다. 서버를 `--port 8002`로 시작하고, 이 문서의 모든 URL도 `8002`로 바꾼다. 다른 프로그램을 임의로 종료할 필요는 없다. |
| `python` 또는 `uvicorn`을 찾을 수 없음 | 가상환경을 활성화했는지 확인한다. 또는 `.\.venv\Scripts\python.exe -m uvicorn ...` 형식으로 실행한다. |
| HTTP 422, `header and process ... do not match` | `header.from.uniquename`와 `process.userId`, `header.to.channelid[0]`와 `process.channelId`가 각각 같은지 확인한다. |
| HTTP 422, `No dummy GAIA service is configured` | 기본 표의 채널 ID를 쓰거나, `DUMMY_CHANNEL_GAIA_MAP`을 설정한 뒤 서버를 다시 시작한다. |
| CUBE 채팅창에 아무 답변이 오지 않음 | 정상이다. 더미 서버는 실제 CUBE API를 호출하지 않는다. `/api/test/outgoing-messages`에서 발송 예정 payload를 확인한다. |

## 서버 종료하기

서버를 실행한 첫 번째 PowerShell 창에서 `Ctrl+C`를 누르면 종료된다. `--reload` 모드로 시작했더라도 같은 방법으로 종료하면 된다.

## 이 가이드에서 확인한 범위

- CUBE callback JSON의 사용자, 채널, 텍스트/선택값 파싱
- 채널별 더미 GAIA Agent 선택
- 사용자 + 채널 기준의 세션 ID 재사용
- GAIA 응답의 마지막 Chat Output에서 최종 답변 추출
- CUBE Rich Notification 발송 payload 생성

실제 CUBE/GAIA 인증, 실제 HTTP 호출, 실제 CUBE 메시지 발송, 영구 세션 저장은 운영용 서버에서 키와 URL을 설정해 검증하는 범위다.
