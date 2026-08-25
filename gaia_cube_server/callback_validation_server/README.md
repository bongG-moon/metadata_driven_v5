# CUBE Callback 검증 서버

이 폴더는 **CUBE callback이 HCP 서버에 도착하고, 서버가 CUBE로 답변을 다시 보낼 수 있는지**만 확인하기 위한 임시 검증 서버다.

GAIA API, Langflow, 대화 세션은 전혀 사용하지 않는다. CUBE에서 어떤 일반 메시지를 보내더라도 서버는 항상 아래 고정 답변을 같은 사용자·채널로 보낸다.

```text
콜백 검증 성공: CUBE 메시지를 정상 수신하고 고정 응답을 보냈습니다.
```

```text
CUBE 사용자 메시지
  -> HCP POST /api/v1/receiver
  -> 고정 Rich Notification 답변을 CUBE로 발송
```

## 가장 중요한 점: 운영 서버와 동시에 실행하지 않는다

등록된 callback URL은 하나다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

따라서 이 검증 서버와 `../production_callback_server/app.py`를 같은 HCP 서비스·같은 URL에서 동시에 실행할 수 없다.

검증할 때만 HCP의 실행 소스를 이 폴더로 바꿔 `app.py`를 기동한다. 고정 답변이 CUBE에 도착한 것을 확인하면, HCP 실행 소스를 다시 `production_callback_server`로 되돌려 실제 GAIA-CUBE 서버를 기동한다. CUBE에 등록된 callback URL 자체는 바꾸지 않는다.

## 필요한 환경변수

`GAIA_API_URL`, `GAIA_AUTH_KEY`는 필요하지 않다. 아래 CUBE 값만 HCP Secret/환경변수 또는 이 폴더의 `.env`에 입력한다.

| 환경변수 | 입력할 값 |
| --- | --- |
| `CUBE_SEND_URL` | `http://cube.skhynix.com:8888/legacy/richnotification` |
| `CUBE_BOT_ID` | CUBE 봇 사번/ID |
| `CUBE_BOT_TOKEN` | CUBE 봇 토큰 |
| `CUBE_BOT_FROMUSERNAME_JSON` | 한글·일본어·영어·중문·기타 순서의 실제 봇 이름 5개 JSON 배열 |
| `CUBE_TIMEOUT_SECONDS` | CUBE 발송 응답 대기 시간(초), 기본값 `20` |

HCP Secret을 사용하지 않는 시험 환경에서는 다음처럼 `.env`를 만든다.

```powershell
Copy-Item .env.example .env
notepad .env
```

`CUBE_BOT_FROMUSERNAME_JSON`은 반드시 실제 봇 표시 이름 다섯 개여야 한다. 빈 문자열이나 `PASTE_...` 값은 서버 기동 시 거부된다.

## HCP에서 기동하기

HCP가 이 폴더의 `app.py`를 실행하도록 배포한 뒤 아래 고정 명령을 사용한다.

```powershell
python app.py
```

실행 코드는 변경하지 않는다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

`0.0.0.0:5000`은 HCP 컨테이너 내부 listen 주소다. CUBE에 등록하는 공개 callback 주소는 위의 HCP URL이며, HCP ingress가 이 앱의 5000 포트로 전달해야 한다.

## 검증 순서

1. HCP가 이 검증 서버를 실행한 상태에서 다음 주소를 연다.

   ```text
   http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/health
   ```

2. 다음과 비슷한 응답이면 HCP 라우팅과 서버 기동은 정상이다.

   ```json
   {
     "status": "ok",
     "mode": "fixed_reply_validation",
     "callback_path": "/api/v1/receiver"
   }
   ```

3. CUBE에서 등록된 챗봇이 있는 승인된 테스트 채널에 아무 일반 메시지나 입력한다.
4. 같은 CUBE 채팅창에 고정 답변이 한 번 도착하는지 확인한다.
5. 확인이 끝나면 HCP 실행 소스를 `production_callback_server`로 복원하고 실제 `app.py`를 다시 배포·기동한다.

`/health` 확인은 CUBE 메시지를 보내지 않는다. 3~4단계는 실제 CUBE 메시지를 보내므로 승인된 테스트 계정과 채널에서만 실행한다.

## callback HTTP 응답과 CUBE 채팅 답변은 다르다

정상 callback 처리 뒤 서버는 CUBE 시스템에 즉시 JSON `null`을 반환한다.

```json
null
```

이 값은 CUBE 시스템을 위한 빠른 접수 확인이다. 사람이 보는 답변은 ACK 뒤 별도로 CUBE Rich Notification API를 통해 발송된 고정 문구다. 따라서 HCP 로그의 `200`뿐 아니라 CUBE 채팅창의 고정 답변도 함께 확인해야 한다.

## 실패했을 때

| 증상 | 확인할 것 |
| --- | --- |
| `/health`가 열리지 않음 | HCP 서비스 기동, ingress가 5000 포트를 가리키는지, callback URL 경로 |
| CUBE 입력 후 HCP 로그가 없음 | CUBE callback URL 등록, CUBE -> HCP 방화벽/허용 IP |
| HCP 로그는 있으나 고정 답변 없음 | `CUBE_SEND_URL`, 봇 ID·토큰, 봇 표시 이름 5개, HCP -> CUBE 방화벽 |
| callback은 `200/null`이나 고정 답변이 없음 | ACK 뒤 CUBE 발송이 실패한 것일 수 있음. CUBE API 응답과 HCP 로그를 확인 |

이 서버는 callback의 사용자 ID와 채널 ID를 header와 process 양쪽에서 비교한다. 채널은 실제 CUBE callback의 `header.from.channelid`, 기존 예시의 `header.to.channelid`, `process.channelId`를 모두 지원한다. 들어온 값이 서로 다르거나 하나의 `channelid` 배열에 서로 다른 값이 있으면 잘못된 callback으로 보고 CUBE로 답변을 보내지 않는다. 일반 질문은 `processdata`에서 읽고, 비어 있으면 `UserSelection`/`SendBtn` 또는 `result.resultdata[].value` 선택값도 수용한다. CUBE 연결용 `!@#HelloChatBot#@!` 요청은 답변 없이 무시한다.

## 코드 자체 점검

다음 명령은 실제 CUBE·GAIA를 호출하지 않고 MockTransport로 callback -> CUBE 발송 흐름을 검사한다.

```powershell
python -m pytest test_app.py -q
python -m compileall -q .
```

실제 토큰과 봇 설정은 `.env` 또는 HCP Secret/환경변수에만 두고 Git, 문서, 로그에 넣지 않는다.
