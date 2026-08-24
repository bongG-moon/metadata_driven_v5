# GAIA-CUBE 서버를 처음 이해하기

이 프로젝트의 목표는 간단하다. 사용자가 CUBE에서 질문하면 GAIA가 답변을 만들고, 그 답변을 다시 같은 CUBE 채팅으로 보내는 것이다.

```text
사용자
  ↓ CUBE에 질문 입력
CUBE
  ↓ callback HTTP POST
HCP GAIA-CUBE 서버
  ↓ GAIA API 호출
GAIA Agent
  ↓ 최종 답변
HCP GAIA-CUBE 서버
  ↓ Rich Notification 발송
CUBE 채팅창의 사용자
```

## 꼭 기억할 주소

서버가 받는 주소는 하나다.

```text
POST http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

이 주소는 CUBE에 등록된 **수신함**이다. 답변을 CUBE에 보여 주는 일은 서버가 별도로 `CUBE_SEND_URL`에 Rich Notification을 보내서 처리한다.

## callback과 답변은 다르다

CUBE가 질문을 전달하면 서버는 callback 호출에 처리 결과를 돌려준다. 이것을 ACK, 즉 “요청을 받았고 처리했다”는 확인으로 이해하면 된다. 현재 최소 구현은 한 요청 안에서 GAIA 호출과 CUBE 발송까지 처리한 뒤 ACK를 반환한다.

```text
callback HTTP 응답 = CUBE 시스템에 주는 처리 확인
CUBE Rich Notification = 사용자가 실제로 보는 GAIA 답변
```

따라서 PowerShell이나 CUBE 시스템에서 `success` JSON을 봤더라도, 실제 답변이 CUBE 채팅창에 나타나는지 별도로 확인해야 한다.

## 서버가 읽는 CUBE 정보

서버는 CUBE callback의 다음 정보를 사용한다.

| 정보 | 대표 위치 |
| --- | --- |
| 누가 질문했는지 | `richnotificationmessage.process.userId` |
| 어느 채널인지 | `richnotificationmessage.process.channelId` |
| 질문 내용 | `richnotificationmessage.process.processdata` |

제공된 callback에 header 정보도 있으면 사용자·채널 ID를 서로 확인한다. 두 값이 다르면 질문을 GAIA에 보내지 않는다.

## GAIA에는 무엇을 보내는가

`.env` 또는 HCP 환경변수의 `GAIA_API_URL`에는 Agent까지 포함한 완성된 URL을 넣는다. 서버는 이 URL을 그대로 사용한다.

```text
GAIA_API_URL=http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
```

질문이 들어오면 GAIA에는 다음 body가 전달된다.

```json
{
  "input_value": "사용자의 질문",
  "user_id": "CUBE 사용자 ID",
  "session_id": "현재 대화 세션 ID"
}
```

GAIA 응답에서는 마지막 Chat Output을 찾고, 그 안의 `results.gaia_response.data.answer`를 우선 답변으로 사용한다.

## 대화가 이어지는 방식

같은 사용자와 같은 CUBE 채널은 같은 GAIA session ID를 재사용한다. 그래서 사용자가 이어서 질문하면 GAIA 쪽에서 대화 문맥을 이어갈 수 있다.

현재 최소 구현은 session ID만 서버 메모리에 보관한다. HCP 앱을 재시작하면 session ID가 사라지고 새 대화가 시작된다. 최근 질문/답변 전문을 저장하거나 조회하는 기능은 아직 없다.

## 직접 질문을 입력해 발송 시험하는 방법

CUBE callback을 만들거나 흉내 내지 않고, 개발자가 질문을 직접 입력해 GAIA와 CUBE 발송을 시험할 수 있다. `production_callback_server/manual_gaia_cube_send.py`는 공개 API가 아닌 사람용 시험 파일이다.

HCP의 `.env` 또는 환경변수가 준비된 상태에서 `production_callback_server/manual_gaia_cube_send.py`를 연다. 파일 맨 위의 아래 값에 실제 시험 정보를 입력한다.

```python
MESSAGE = "GAIA-CUBE 실제 연동 테스트입니다. 한 줄로 인사해 주세요."
RECEIVER_ID = "AUTHORIZED_EMPLOYEE_ID"
# 사번으로만 발송할 때는 비워 둔다.
CHANNEL_ID = ""

GAIA_USER_ID = ""  # GAIA 권한 사번이 따로 있을 때만 입력
SESSION_ID = ""    # 같은 직접 시험 대화를 이어갈 때만 입력
```

키와 토큰은 코드에 넣지 않는다. `.env` 또는 HCP Secret/환경변수에만 넣는다. 값을 저장한 뒤 HCP 실행 환경의 `production_callback_server` 폴더에서 아래 한 줄을 실행한다.

```powershell
python manual_gaia_cube_send.py
```

이 명령은 실제로 다음을 수행한다.

```text
직접 입력한 질문 → GAIA 실행 → 답변 추출 → CUBE 메시지 발송
```

`RECEIVER_ID`는 기본적으로 GAIA 사용자 ID와 CUBE 수신자에 사용한다. `CHANNEL_ID`는 선택 사항이며, 사번으로만 발송할 때는 비워 둔다. `GAIA_USER_ID`를 비워 두면 `RECEIVER_ID`가 GAIA 사용자 ID가 된다. `SESSION_ID`를 비워 두면 새 대화를 시작한다. 실제 외부 시스템에 메시지를 보내므로 승인된 테스트 사용자만 사용한다. 성공 여부는 PowerShell 출력과 수신자 CUBE 양쪽에서 확인한다.

## callback 형식으로 직접 시험하는 방법 (선택)

실제 CUBE 등록 전에도 HCP callback URL에 CUBE 형식의 POST를 보내면 다음 전체 흐름을 시험할 수 있다.

```text
PowerShell POST → HCP callback → GAIA → CUBE 답변 발송
```

이 시험은 실제 CUBE 채널에 메시지를 보낸다. 따라서 승인된 사용자 ID와 테스트 채널만 사용해야 한다. 이는 CUBE가 보내는 callback 형식을 확인할 때만 사용한다. 복사해 실행할 PowerShell 명령과 확인 방법은 [production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md](production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)의 “callback 형식 전체 흐름 시험”에 있다.

## 일부러 넣지 않은 기능

현재는 이해하고 첫 연동을 하기 위한 최소 서버다.

- 공개 메시지 발송 endpoint 없음 (`manual_gaia_cube_send.py`는 공개 API가 아닌 사람용 시험 도구)
- 별도 로컬/운영 모드 없음
- 데이터베이스, 작업 큐, 스케줄러 없음
- 최근 대화 조회 API 없음
- 자동 재시도와 중복 callback 처리 정책 없음

이 기능들은 실제 CUBE 인증, 재전송, 메시지 중복 규칙이 확인된 뒤 필요에 맞게 추가한다.
