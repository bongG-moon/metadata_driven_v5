# HCP GAIA-CUBE Callback Server

이 폴더는 HCP에서 실행되는 하나의 콜백 서버다. 사용자가 CUBE 채널에 질문하면 서버가 GAIA에 질문을 전달하고, GAIA의 최종 답변을 다시 CUBE 채팅창으로 보낸다.

```text
CUBE 사용자 질문
  → HCP의 POST /api/v1/receiver
  → 즉시 HTTP 200 + JSON null ACK
  → GAIA_API_URL 호출
  → GAIA 최종 답변 추출
  → CUBE Rich Notification 발송
```

## 고정 주소

서버가 받는 실제 callback 경로는 하나뿐이다.

```text
POST /api/v1/receiver
```

등록된 HCP callback URL:

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

별도의 공개 메시지 발송 endpoint는 제공하지 않는다. 임의의 외부 요청이 봇을 메시지 중계기로 사용하는 일을 막기 위한 것이다. 대신 개발자가 직접 질문을 넣어 GAIA 실행과 CUBE 발송만 시험할 때는 명령줄 도구 `manual_gaia_cube_send.py`를 사용한다. 이 도구는 HTTP endpoint가 아니며, HCP 실행 환경의 서버 폴더에서 사람이 실행한다.

## 필요한 설정

`.env.example`을 복사하거나 HCP Secret/환경변수에 아래 값을 넣는다.

| 설정 | 의미 |
| --- | --- |
| `GAIA_API_URL` | **완성된** GAIA Agent API URL. Agent 식별자까지 포함한 전체 주소를 그대로 넣는다. |
| `GAIA_AUTH_KEY` | GAIA 인증 키 |
| `CUBE_SEND_URL` | CUBE Rich Notification 발송 전체 URL |
| `CUBE_BOT_ID`, `CUBE_BOT_TOKEN` | CUBE 봇 인증 정보 |
| `CUBE_BOT_FROMUSERNAME_JSON` | 한글·일본어·영어·중문·기타 순서의 봇 이름 5개 JSON 배열 |

예를 들어 `GAIA_API_URL`에는 다음처럼 전체 URL을 넣는다.

```dotenv
GAIA_API_URL=http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
```

서버는 이 값을 조합하거나 변경하지 않고 그대로 호출한다.

## 실행과 확인

HCP에서는 `app.py`를 실행한다. 이때 같은 폴더의 `markdown_rich_notification.py`도 함께 배포해야 한다. 이 파일이 GAIA Markdown을 CUBE Rich Notification의 `body.row`로 바꾸며, 봇 정보·수신자·`process`·CUBE 전송 API는 계속 `app.py`가 담당한다. 앱의 실행 설정은 아래처럼 고정되어 있다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

`0.0.0.0:5000`은 HCP 컨테이너 내부의 listen 주소다. CUBE에 등록하는 주소는 위의 HCP URL이며, HCP ingress가 그 요청을 컨테이너의 5000 포트로 전달해야 한다.

기동 상태는 다음 URL로 확인한다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/health
```

처음 실제 연동하는 순서는 [PRODUCTION_SERVER_RUN_GUIDE.md](PRODUCTION_SERVER_RUN_GUIDE.md)를 따른다. 이 문서에는 HCP 설정, 직접 질문 입력으로 하는 GAIA→CUBE 발송 시험, 수동 callback 시험, ACK와 실제 CUBE 답변의 차이가 순서대로 설명되어 있다.

## 직접 GAIA→CUBE 발송 시험

HCP의 `.env`/환경변수 설정이 준비된 뒤 `manual_gaia_cube_send.py`를 연다. 파일 맨 위의 `MESSAGE`, `RECEIVER_ID`를 실제 시험 값으로 바꾼다. `CHANNEL_ID`는 채널에도 보내야 할 때만 입력하고, 사번으로만 발송할 때는 비워 둔다. GAIA 권한 사번이 다를 때만 `GAIA_USER_ID`를 입력하고, 이전 직접 시험의 대화를 이어갈 때만 `SESSION_ID`를 입력한다.

인증 키와 토큰은 코드에 적지 않고 `.env` 또는 HCP Secret/환경변수에만 둔다. 값 입력 후 HCP 실행 환경의 이 폴더에서 다음 한 줄을 실행한다.

```powershell
python manual_gaia_cube_send.py
```

이 시험은 callback을 받지 않는다. 입력한 질문으로 GAIA를 실제 호출한 뒤, 나온 답변을 지정한 CUBE 수신자 사번으로 실제 발송한다. `CHANNEL_ID`를 입력한 경우에는 해당 채널도 사용한다. 따라서 허가된 테스트 대상만 사용하고, 결과는 PowerShell 출력뿐 아니라 수신자 CUBE에서 확인한다. 세부 안내는 [MANUAL_SEND_TOOL_README.md](MANUAL_SEND_TOOL_README.md)를 따른다.

## GAIA 답변의 Rich Notification 변환 미리보기

GAIA 답변의 제목, 목록, Markdown 표, 이미지, 링크가 CUBE의 `label`, `grid`, `image`, `hypertext`로 어떻게 바뀌는지 먼저 확인하려면 아래 명령을 실행한다. 외부 API를 호출하지 않으며, 키·토큰도 사용하지 않는다.

```powershell
python rich_notification_preview.py
```

생성되는 `preview_output\gaia_to_cube_rich_notification_preview.html`을 브라우저로 열면 화면 모양을, `.json` 파일을 열면 CUBE로 보낼 `body` JSON을 볼 수 있다. 변환 규칙과 실제 발송 범위는 [RICH_NOTIFICATION_RENDERING_GUIDE.md](RICH_NOTIFICATION_RENDERING_GUIDE.md)를 따른다.

이전 변환 방식과 현재 방식의 차이를 같은 Markdown 입력으로 비교하려면 `python markdown_renderer_comparison.py`를 실행하고, 생성된 `preview_output\markdown_renderer_comparison.html`을 연다.

두 변환기를 실제 callback 서버로 각각 시험하려면 [RENDERER_CASES_GUIDE.md](RENDERER_CASES_GUIDE.md)를 따른다. 환경설정으로 고르지 않고 `app_case_legacy.py` 또는 `app_case_production.py` 중 하나만 실행한다.

## 현재 최소 구현의 범위

- 같은 `사용자 ID + CUBE 채널 ID`에는 같은 GAIA `session_id`를 재사용한다.
- 세션 ID는 서버 메모리에만 보관한다. HCP 앱이 재시작되면 새 세션이 시작된다.
- GAIA 처리 실패 후 CUBE fallback 안내문에는 `GAIA 응답 시간 초과`, `GAIA API 연결/응답 오류`, `Langflow 최종 답변 없음`처럼 안전하게 분류한 원인과 재시도 안내를 함께 보낸다. 내부 URL·HTTP 상세 오류·예외 원문은 보내지 않는다.
- 유효한 callback은 GAIA 실행 전에 `200`과 JSON `null`을 즉시 반환한다. 이후 GAIA 답변·fallback 발송 실패는 서버 로그에서 확인한다. 이 구조는 CUBE가 오래 기다리다 기본 안내를 표시하는 일을 줄이기 위한 것이다.
- 최근 대화 전문, MongoDB, 별도 작업 큐, 자동 재시도, 스케줄러, 대화 조회 API는 이 최소 서버에 포함하지 않는다. GAIA/CUBE 처리는 FastAPI의 프로세스 내 백그라운드 작업으로 실행되므로 HCP 앱이 재시작되면 진행 중이던 요청은 보장되지 않는다.
- callback 인증 방식, 재전송 정책, CUBE 발송 성공 body는 담당 가이드가 확인되면 추가해야 한다.

실제 키와 토큰은 `.env`, HCP Secret 또는 환경변수에만 보관하고 Git, 로그, 문서에 넣지 않는다.
