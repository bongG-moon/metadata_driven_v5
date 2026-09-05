# PTMORE PKG Agent Portal (Flask)

기존 `ptmore_portal`의 화면, MongoDB 저장, Phoenix 사용 이력, 메타데이터 API, 스케줄 API를 유지하면서 HTTP 서버와 로그인 세션만 Flask로 바꾼 폴더입니다.

로컬 화면 확인용 mock 로그인은 명시적으로 선택해야 합니다.

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
```

주신 HCP WebApp 기본 구조처럼 `index.py`가 `web_main.py`의 Flask 객체를 불러 실행합니다. `web_main.py`에는 별도 실행 코드나 포트 설정을 두지 않습니다.

운영 WebApp은 로컬 `.env`를 읽지 않습니다. HCP Secret/프로세스 환경변수가 있으면 우선 사용하고, 없는 값은 배포 폴더의 `portal_runtime_config.py` Python 변수에서 읽습니다.

개발 PC에서 동일한 구조로 화면을 확인할 때도 `index.py`를 실행합니다.

```powershell
.\.venv\Scripts\python.exe index.py
```

`portal_core.py`는 기존 Portal의 MongoDB·Phoenix·메타데이터 업무 규칙을 그대로 재사용하는 호환 모듈입니다. HTTP 서버는 `web_main.py`의 Flask이며, 이 폴더에는 Uvicorn 실행 코드를 두지 않았습니다.

이 Flask 버전은 사용자 이름을 MongoDB에서 다시 찾지 않습니다. 로그인에서 확보한 `session['emp_no']`, `session['emp_name']`만 권한·소유자·화면 표시의 기준으로 사용합니다.

## Python 설정 파일

운영 환경이 Git 기반이 아니거나 HCP Secret 설정을 사용할 수 없을 때는 아래처럼 배포 폴더에서만 설정 파일을 만듭니다.

```powershell
Copy-Item portal_runtime_config.example.py portal_runtime_config.py
```

그 뒤 [portal_runtime_config.example.py](portal_runtime_config.example.py)의 예시를 참고해 `portal_runtime_config.py`를 수정합니다. 예를 들어 목록/JSON 값도 Python 문법 그대로 입력할 수 있습니다.

```python
PTMORE_PORTAL_FLASK_AUTH_MODE = "sso"
MONGODB_URI = "mongodb://계정:비밀번호@호스트:27017/?authSource=admin"
PTMORE_PHOENIX_PROJECTS_JSON = ["Phoenix 프로젝트명 1", "Phoenix 프로젝트명 2"]
```

설정 우선순위는 다음과 같습니다.

1. HCP Secret 또는 프로세스 환경변수의 비어 있지 않은 값
2. `portal_runtime_config.py`의 같은 이름 Python 변수
3. 코드의 안전한 기본값

`portal_runtime_config.py`는 `.gitignore`에 등록되어 있어 개발 저장소에 커밋되지 않습니다. 운영 서버에서는 Git을 쓰지 않더라도 이 파일을 `web_main.py`와 같은 폴더에 두고 직접 수정하면 됩니다. 값을 바꾼 뒤에는 WebApp을 재시작해야 합니다.

이 Flask 버전은 `.env`와 `.env.example`을 사용하지 않습니다. 설정 목록과 예시는 `portal_runtime_config.example.py` 한 파일에서 확인합니다.

## 운영 SSO 전환

운영 HCP 환경에 `hcputil.auth.sso`가 설치된 뒤 `portal_runtime_config.py`에서는 아래처럼 **Python 문법으로** 설정합니다.

```python
PTMORE_PORTAL_FLASK_AUTH_MODE = "sso"
```

HCP Secret 화면에 입력하는 경우에만 `KEY=value` 형식을 사용합니다.

Flask 세션 키는 서버가 시작될 때 `os.urandom()`으로 생성합니다. 따라서 WebApp을 재시작하면 기존 로그인 세션은 다시 로그인해야 합니다.

`/login`은 제공받은 Flask 패턴대로 `SSO(request)`로 사번과 이름을 읽어 `session['emp_no']`, `session['emp_name']`에 저장합니다. Portal 권한과 스케줄 소유자 판단은 이 서버 세션 값만 사용합니다.

## 실행 진입점

주신 기본 Flask 코드와 동일하게 실행 구조는 아래와 같습니다.

```python
# index.py
from web_main import app as application

if __name__ == "__main__":
    application.run(debug=True, host="0.0.0.0")
```

실제 Flask 앱과 모든 Route는 `web_main.py`의 `app = Flask(__name__)`에 있습니다.

## 설정

기존 Portal과 같은 MongoDB·Phoenix·메타데이터 API 설정 예시는 `portal_runtime_config.example.py`에 있습니다. 실제 비밀번호·API 키는 HCP Secret 또는 Git에서 제외된 `portal_runtime_config.py`에만 넣습니다.

메타데이터 등록 API는 유형별 Flow 주소 세 개와 API 키 헤더 이름·키만 설정합니다.

```python
PTMORE_METADATA_API_AUTH_HEADER = "X-Gaia-Auth-Key"
PTMORE_METADATA_API_AUTH_KEY = "발급받은-API-키"
PTMORE_METADATA_TABLE_CATALOG_API_URL = "https://.../table-catalog"
PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL = "https://.../main-flow-filter"
PTMORE_METADATA_DOMAIN_API_URL = "https://.../domain"
```

각 Flow의 노드 이름은 Portal 내부의 고정 매핑을 사용하므로 별도 설정하지 않습니다.

메타데이터 목록 화면에서 MongoDB에 이미 저장된 등록 결과를 읽기 전용으로 표시할 때만 아래 두 설정을 사용합니다.

```python
PTMORE_METADATA_LIVE_READ_MODE = "configured"
PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON = {
    "domain": "agent_v4_test_domain_items",
    "table_catalog": "agent_v4_test_table_catalog_items",
    "main_flow_filters": "agent_v4_test_main_flow_filters",
}
```

이 설정은 Portal의 목록 표시 전용입니다. Portal은 Flow API 호출에 MongoDB URI·데이터베이스·컬렉션 등의 설정을 보내지 않으며, Flow의 MongoDB 연결 설정을 덮어쓰지 않습니다.
