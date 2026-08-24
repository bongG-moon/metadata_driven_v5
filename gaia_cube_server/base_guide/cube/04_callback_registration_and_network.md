# CUBE Callback 등록 및 HCP 네트워크 가이드

## 현재 등록할 callback URL

이 프로젝트의 CUBE callback 수신 경로는 하나다.

```text
POST /api/v1/receiver
```

사용자가 신청한 HCP callback URL은 다음과 같다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

CUBE에는 위 전체 URL을 등록한다. `0.0.0.0`은 앱이 내부에서 listen하는 주소일 뿐 CUBE에 등록하는 주소가 아니다.

## Callback URL과 답변 발송 URL의 차이

```text
사용자 → CUBE에 질문
CUBE → HCP callback URL로 POST
HCP 서버 → GAIA API 호출
HCP 서버 → CUBE Rich Notification API로 답변 발송
```

callback URL은 질문을 받는 수신함이다. 사용자가 보는 답변은 `CUBE_SEND_URL`로 Rich Notification을 전송한 뒤에 나타난다.

현재 확인된 CUBE 발송 전체 URL은 다음과 같다.

```text
http://cube.skhynix.com:8888/legacy/richnotification
```

실제 배포 환경에서는 이 값을 `CUBE_SEND_URL`에 설정한다. 토큰, 봇 ID, 사용자 사번은 문서나 방화벽 신청서의 불필요한 곳에 적지 않는다.

## HCP 포트 연결

애플리케이션의 실행 코드는 아래처럼 고정되어 있다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

HCP ingress는 외부 callback URL의 `/api/v1/receiver` 요청을 앱의 내부 5000 포트로 전달해야 한다. 등록 URL에 포트가 없는 경우 CUBE는 HTTP 기본 포트로 접속하고, HCP가 내부 5000 포트로 연결한다.

배포 후 다음 주소가 열리는지 확인한다.

```text
http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/health
```

## 방화벽 방향

두 방향이 모두 필요하다.

| 통신 방향 | 목적 | 확인할 값 |
| --- | --- | --- |
| CUBE → HCP 서비스 | 사용자 질문 callback | 등록 callback URL, CUBE의 허용 송신 IP, HCP ingress 허용 정책 |
| HCP 서비스 → CUBE | GAIA 답변 Rich Notification 발송 | `CUBE_SEND_URL`, CUBE 8888 포트, HCP egress 허용 정책 |

제공받은 CUBE 안내에는 callback 출발지 예시로 개발 `10.158.122.139`, 운영 `10.158.25.30 ~ 42`가 있었다. 어느 CUBE 환경이 HCP 서비스에 callback을 보내는지와 정확한 방화벽 신청값은 CUBE/보안 담당자에게 확인한 뒤 적용한다.

## HCP 설정값

HCP Secret 또는 환경변수에 아래 값을 넣는다.

```text
GAIA_API_URL
GAIA_AUTH_KEY
CUBE_SEND_URL
CUBE_BOT_ID
CUBE_BOT_TOKEN
CUBE_BOT_FROMUSERNAME_JSON
```

`GAIA_API_URL`에는 Agent까지 포함한 완성된 URL을 직접 넣는다. 서버는 이 URL을 조합하지 않는다.

```text
http://gaia.api.skhynix.com/v2/agents/<GAIA_AGENT_ID>/external
```

## 등록 뒤의 시험

1. HCP `/health`가 정상인지 확인한다.
2. 승인된 사용자와 테스트 CUBE 채널로 PowerShell callback POST를 한 번 보낸다.
3. PowerShell의 처리 결과와 CUBE 채팅창의 실제 답변을 모두 확인한다.
4. CUBE에 callback URL을 등록한 뒤, CUBE 채널에서 같은 시험을 반복한다.

수동 POST는 실제 GAIA와 CUBE를 호출한다. 복사해 실행할 명령은 [운영 서버 실행 가이드](../../production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)에 있다.

## 아직 확인이 필요한 계약

- callback 인증/서명 방식
- callback timeout과 재전송 규칙
- CUBE 발송 API의 성공/실패 body와 중복 발송 정책
- HCP ingress와 CUBE 사이의 정확한 허용 IP/포트
- CUBE 사용자 ID가 GAIA 권한 사번으로 사용 가능한지

`base_guide/cube/source/`의 원본 파일은 당시 제공된 자료 보관용이다. 현재 실행 기준은 이 문서와 `production_callback_server`의 가이드다.
