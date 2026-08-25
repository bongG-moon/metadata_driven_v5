# GAIA-CUBE Server

이 폴더는 CUBE 질문을 GAIA로 전달하고, GAIA 답변을 CUBE로 되돌려 보내는 HCP callback 서버다.

```text
CUBE → HCP callback server → GAIA → CUBE
```

현재 실제 callback 주소:

```text
POST http://aiu-pkg-prod-ai-api001-basic-dev.api.hcpd03.skhynix.com/api/v1/receiver
```

## 먼저 읽을 문서

- 처음 개념을 이해하려면: [START_HERE_CALLBACK_FLOW_GUIDE.md](START_HERE_CALLBACK_FLOW_GUIDE.md)
- HCP 설정, 직접 질문 입력으로 하는 GAIA→CUBE 발송 시험, callback 연동 시험: [production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md](production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)
- 서버 폴더의 간단한 안내: [production_callback_server/README.md](production_callback_server/README.md)
- GAIA를 거치지 않고 CUBE callback·재발송만 먼저 검증하려면: [callback_validation_server/README.md](callback_validation_server/README.md)
- 제공받은 GAIA/CUBE 원본 가이드와 정리 자료: [base_guide/README.md](base_guide/README.md)

## 현재 구성

| 위치 | 역할 |
| --- | --- |
| `production_callback_server/app.py` | HCP에서 실행되는 FastAPI callback 서버 |
| `production_callback_server/.env.example` | 실제 키 없이 설정 형식만 제공하는 템플릿 |
| `production_callback_server/manual_gaia_cube_send.py` | callback 없이 직접 입력한 질문을 GAIA에 보내고 CUBE로 답변을 발송하는 사람용 시험 도구 |
| `production_callback_server/test_app.py` | GAIA/CUBE HTTP 호출을 mock으로 바꾼 흐름 테스트 |
| `callback_validation_server/app.py` | GAIA 호출 없이 CUBE callback을 받으면 고정 답변만 CUBE로 되돌리는 HCP 임시 검증 서버 |
| `base_guide/` | 사용자가 제공한 API 계약과 참고 자료 |

운영 서버에는 하나의 callback 경로만 있으며, 공개 메시지 발송 endpoint는 없다. `GAIA_API_URL`에는 GAIA Agent까지 포함한 전체 URL을 직접 설정한다.

개발자가 실제 GAIA→CUBE 흐름만 확인할 때는 `production_callback_server/manual_gaia_cube_send.py` 파일 맨 위에 `MESSAGE`, `RECEIVER_ID`를 입력한다. `CHANNEL_ID`는 채널에도 보내야 할 때만 입력하고, 사번으로만 발송할 때는 비워 둔다. 필요할 때만 `GAIA_USER_ID`, `SESSION_ID`도 입력한다. 인증 키와 토큰은 코드가 아니라 `.env` 또는 HCP Secret/환경변수에만 둔다.

값을 저장한 뒤 HCP 실행 환경의 서버 폴더에서 아래 한 줄을 실행한다. 이 시험은 실제 외부 호출과 실제 CUBE 메시지 발송을 수행하므로 승인된 수신자와 채널만 사용한다.

```powershell
python manual_gaia_cube_send.py
```

실제 키, 토큰, 사번은 `.env` 또는 HCP Secret/환경변수에만 넣고 Git이나 문서에 남기지 않는다.

`callback_validation_server`는 실제 GAIA 서버와 동시에 실행하지 않는다. 등록된 callback URL은 같으므로, HCP에서 검증 서버를 잠시 배포해 고정 답변을 확인한 뒤 `production_callback_server`로 되돌린다.
