# PTMORE PKG Agent Portal (Flask)

기존 `ptmore_portal`의 화면, MongoDB 저장, Phoenix 사용 이력, 메타데이터 API, 스케줄 API를 유지하면서 HTTP 서버와 로그인 세션만 Flask로 바꾼 폴더입니다.

현재 기본 로그인은 테스트용 Flask 세션입니다.

- 사번: `2069026`
- 이름: `문봉건`
- 프로필 사진: `http://skynet.skhynix.com/portalWeb/uploadfile/pictures/2069026.jpg`

## 로컬 실행

PowerShell에서 아래 순서로 실행합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\ptmore_portal_flask
C:\Python313\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

브라우저에서 `http://127.0.0.1:5000`을 엽니다. Flask 기본 실행 방식이므로 Uvicorn 명령은 사용하지 않습니다.

`portal_core.py`는 기존 Portal의 MongoDB·Phoenix·메타데이터 업무 규칙을 그대로 재사용하는 호환 모듈입니다. HTTP 서버는 `app.py`의 Flask뿐이며, 이 폴더에는 Uvicorn 실행 코드를 두지 않았습니다.

이 Flask 버전은 사용자 이름을 MongoDB에서 다시 찾지 않습니다. 로그인에서 확보한 `session['emp_no']`, `session['emp_name']`만 권한·소유자·화면 표시의 기준으로 사용합니다.

## 운영 SSO 전환

운영 HCP 환경에 `hcputil.auth.sso`가 설치된 뒤 `.env` 또는 HCP Secret에서 아래처럼 바꿉니다.

```dotenv
PTMORE_PORTAL_FLASK_AUTH_MODE=sso
PTMORE_FLASK_SESSION_SECRET=충분히_긴_임의의_비밀값
```

`/login`은 제공받은 Flask 패턴대로 `SSO(request)`로 사번과 이름을 읽어 `session['emp_no']`, `session['emp_name']`에 저장합니다. Portal 권한과 스케줄 소유자 판단은 이 서버 세션 값만 사용합니다.

## 실행 진입점

`app.py`에는 일반 Flask 실행용 `app`과 WSGI 배포용 `application`이 모두 있습니다.

```python
application = app
```

HCP가 WSGI 객체를 요구하면 `app:application`을 지정하면 됩니다. 로컬 확인은 위의 `python app.py`만 사용하면 됩니다.

## 설정

기존 Portal과 같은 MongoDB·Phoenix·메타데이터 API 환경변수는 `.env.example`에 포함되어 있습니다. 실제 비밀번호·API 키는 `.env` 또는 HCP Secret에만 넣고 Git에 추가하지 않습니다.
