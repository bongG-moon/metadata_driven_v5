# GaiA Floating Chat 로컬 테스트

이 폴더는 현재 PTMORE Portal과 분리된 로컬 테스트용입니다. 브라우저에서 GaiA External 인증키를 노출하지 않고, FastAPI가 실제 GaiA External API JSON 요청을 External Gateway로 전달합니다.

`gaia-floating-chat` 공식 패키지는 아직 이 폴더에 포함하지 않았습니다. 배포 패키지나 저장소 주소를 받기 전에도 Gateway URL, 인증키, 사용자 사번, 세션, 일반 JSON 응답이 정상인지 먼저 검증할 수 있도록 간단한 플로팅 채팅 화면을 구현했습니다.

현재 요청 본문은 다음과 같이 실제 동작 확인에 사용된 External API 형식으로 고정했습니다.

```json
{
  "input_value": "사용자 질문",
  "session_id": "portal-floating-사번-랜덤값",
  "tweaks": {
    "GaiA Input": {
      "metadata": "{\"user_id\":\"사번\"}"
    }
  }
}
```

## 실행

PowerShell에서 아래 순서로 실행합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_floating_chat_local_test
C:\Python313\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe app.py
```

가상환경 활성화 명령은 사용하지 않으므로 PowerShell 실행 정책을 변경할 필요가 없습니다.

`.env`에는 다음 세 값만 반드시 채우면 됩니다.

```dotenv
GAIA_EXTERNAL_AGENT_URL=https://.../v2/agents/<agentId>/external/
GAIA_EXTERNAL_API_KEY=AI_Market에서_발급받은_키
GAIA_TEST_USER_ID=본인사번7자리
```

서버가 실행되면 브라우저에서 아래 주소를 엽니다.

```text
http://127.0.0.1:8003
```

오른쪽 아래의 `GaiA Agent` 버튼을 눌러 질문을 보냅니다. 실제 전송 본문과 응답 원문은 채팅창의 `요청·응답 원문 확인`을 펼쳐 확인할 수 있습니다.

## 확인 순서

1. `http://127.0.0.1:8003/health`에서 `configured: true`인지 확인합니다.
2. 화면의 연결 상태가 `전송 준비 완료`인지 확인합니다.
3. 간단한 질문을 전송합니다.
4. 403이면 AI Market External 인증키 또는 `GAIA_TEST_USER_ID` 권한을 확인합니다.
5. 502이면 Gateway URL, 사내망 연결, 인증서 설정을 확인합니다.

## 세션 동작

- `GAIA_TEST_SESSION_ID`가 비어 있으면 브라우저 탭마다 `portal-floating-사번-랜덤값` 세션을 자동 생성합니다.
- 같은 탭에서 페이지를 새로고침해도 세션을 유지합니다.
- 고정 세션을 검증하려면 `.env`의 `GAIA_TEST_SESSION_ID`에 직접 값을 넣고 서버를 다시 시작합니다.

## 보안 주의

- `.env`는 Git에 올리지 않습니다.
- External 인증키는 브라우저로 반환하지 않습니다.
- 이 화면은 로컬 연결 검증용이며, 실제 Portal 반영 전에는 AI Market 담당자에게 브라우저 직접 키 노출 허용 여부와 External Gateway의 CORS·프록시 정책을 확인해야 합니다.
