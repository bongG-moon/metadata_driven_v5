# 처음 보는 사람을 위한 GAIA-CUBE 서버 안내

## 이 서버는 무엇을 하나요?

CUBE는 사내 메신저이고, GAIA는 질문에 답하는 Agent가 실행되는 환경이다. 이 서버는 두 시스템 사이의 전달자다.

```text
1. 사용자가 CUBE에 질문한다.
2. CUBE가 HCP 서버에 “이 사용자가 이런 질문을 했다”라고 알려 준다.
3. HCP 서버가 GAIA에 질문한다.
4. GAIA가 만든 답변을 HCP 서버가 CUBE에 보낸다.
5. 사용자는 원래 CUBE 채팅창에서 답변을 본다.
```

## 자주 헷갈리는 단어

| 단어 | 쉬운 설명 |
| --- | --- |
| API | 프로그램끼리 정해진 형식으로 대화하는 통로 |
| callback | CUBE가 우리 서버에 사용자 질문을 알려 주는 전화와 같은 요청 |
| endpoint | 요청을 받는 정확한 URL 주소 |
| GAIA session ID | 같은 사용자의 이어지는 질문을 GAIA가 같은 대화로 알 수 있게 하는 표식 |
| ACK | CUBE 시스템에 돌려주는 “요청 처리 결과를 받았다”는 확인 |

## 이 프로젝트의 endpoint

등록된 HCP endpoint는 다음 하나다.

```text
POST http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

이 URL은 사용자가 직접 답변을 받아 보는 웹페이지가 아니다. CUBE가 질문을 전달하는 수신함이다. 사용자가 보는 실제 답변은 서버가 CUBE의 Rich Notification API로 별도 발송한다.

## 실제로 필요한 설정

서버를 실행하려면 HCP 환경변수 또는 `.env`에 다음 값이 있어야 한다.

```text
GAIA_API_URL
GAIA_AUTH_KEY
CUBE_SEND_URL
CUBE_BOT_ID
CUBE_BOT_TOKEN
CUBE_BOT_FROMUSERNAME_JSON
```

가장 중요한 값은 `GAIA_API_URL`이다. 이 값에는 GAIA의 기본 주소만 넣는 것이 아니라, 호출할 Agent까지 포함한 전체 URL을 넣는다.

```text
http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
```

## 메시지를 시험하려면

PowerShell에서 callback과 같은 형식의 POST를 HCP URL로 보낼 수 있다. 그러면 테스트 요청도 실제와 똑같이 GAIA를 호출하고 CUBE 채팅으로 답변을 보낸다.

```text
PowerShell → HCP 서버 → GAIA → CUBE 채팅창
```

이 시험은 실제 메시지를 발생시키므로 본인 또는 승인된 사번과 테스트 채널을 사용해야 한다. 복사해서 실행할 명령은 [production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md](production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)에 있다.

## 대화는 어떻게 이어지나요?

서버는 `사용자 ID + 채널 ID`마다 GAIA session ID 하나를 메모리에 기억한다. 같은 사람이 같은 채널에서 이어서 질문하면 그 session ID를 다시 보내므로 GAIA가 대화를 이어갈 수 있다.

현재는 session ID만 메모리에 보관한다. HCP 앱을 재시작하면 대화는 새로 시작하며, 최근 질문과 답변을 보여 주는 기능은 아직 없다.

## 지금 단계에서 하지 않는 일

- 공개 메시지 발송 API 제공
- 별도 실행 모드 선택
- 데이터베이스 저장
- 대화 기록 조회
- 자동 재시도, 중복 callback 처리, 스케줄링

이런 기능은 CUBE의 인증, 재전송, 중복 메시지 규칙이 확정된 뒤 필요한 범위에서 추가한다.
