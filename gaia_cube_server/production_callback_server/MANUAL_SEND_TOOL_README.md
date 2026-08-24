# 직접 GAIA → CUBE 발송 시험 도구

이 도구는 CUBE callback을 받지 않는다. 사람이 코드에 질문과 수신 대상을 입력한 뒤 실행하면, GAIA 답변을 CUBE로 실제 발송한다.

```text
코드에 질문 입력 → GAIA 호출 → 최종 답변 추출 → CUBE 메시지 발송
```

## 포함 파일

- `manual_gaia_cube_send.py`: 직접 시험 도구
- `app.py`: GAIA 호출, 답변 추출, CUBE 발송 기능을 공통으로 제공하는 코드
- `.env.example`: 실제 키 없이 설정 형식만 제공하는 템플릿
- `requirements.txt`: 필요한 Python 패키지 목록

`app.py` 서버를 실행할 필요는 없다. 이 시험은 `manual_gaia_cube_send.py`만 실행한다.

## 처음 한 번만 준비하기

1. HCP 실행 환경의 폴더에 압축을 푼다.
2. 필요한 패키지를 설치한다.

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. `.env.example`을 `.env`로 복사한다.

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

4. `.env`에 실제 `GAIA_API_URL`, `GAIA_AUTH_KEY`, `CUBE_SEND_URL`, CUBE 봇 ID·토큰·표시 이름을 입력한다.

   GAIA/CUBE 인증 키와 토큰은 **오직 `.env` 또는 HCP Secret/환경변수**에만 넣는다. `manual_gaia_cube_send.py`에는 넣지 않는다.

## 매번 시험할 때 할 일

1. `manual_gaia_cube_send.py`를 메모장 또는 편집기로 연다.
2. 파일 맨 위의 직접 시험 값 5개를 입력하거나 필요에 맞게 수정한다.

   ```python
   MESSAGE = "오늘 생산 현황을 알려줘"
   RECEIVER_ID = "CUBE_답변_수신자_사번"
   # 사번으로만 발송할 때는 비워 둔다.
   CHANNEL_ID = ""

   # 비워 두면 RECEIVER_ID를 GAIA 사용자 ID로 사용한다.
   GAIA_USER_ID = ""

   # 비워 두면 매번 새 GAIA 대화를 시작한다.
   SESSION_ID = ""
   ```

   - `MESSAGE`: GAIA에 보낼 질문
   - `RECEIVER_ID`: CUBE 답변을 받을 사용자 사번
   - `CHANNEL_ID`: 선택 사항. CUBE 채널에도 보내야 할 때만 입력하며, 사번으로만 발송할 때는 비워 둔다.
   - `GAIA_USER_ID`: GAIA 권한 사번이 수신자 사번과 다를 때만 입력
   - `SESSION_ID`: 이전 직접 시험 대화를 이어갈 때만 입력

3. 같은 폴더에서 아래 한 줄만 실행한다.

   ```powershell
   python manual_gaia_cube_send.py
   ```

성공하면 PowerShell에 GAIA session ID와 전송한 답변이 출력되고, 수신자 사번의 CUBE에 답변이 도착한다. `CHANNEL_ID`를 입력한 경우 해당 채널도 대상으로 사용한다. 실제 메시지를 전송하므로 승인된 테스트 수신자만 사용한다.
