# metadata_driven_v5 HTML Report API

이 폴더는 v5의 `00 HTML 시각화 생성기`가 만든 self-contained HTML/SVG를 로컬 파일로 저장하고, 사용자에게 보기·다운로드 링크를 발급하는 작은 FastAPI 서버입니다.

- `POST /reports`: HTML 저장 및 링크 발급
- `GET /reports/view/{report_id}`: 브라우저에서 보기
- `GET /reports/download/{report_id}`: HTML 파일 다운로드
- `DELETE /reports/{report_id}`: 수동 삭제
- `GET /`, `GET /health`: 상태 확인

MongoDB와 `.env`는 사용하지 않습니다. HTML 생성과 분석은 Langflow가 담당하고, 이 서버는 저장·만료·링크 전달만 담당합니다.

## 1. 보안 기본값

기본 설정은 같은 PC에서 체험하는 용도입니다.

- `127.0.0.1`에만 바인딩하므로 다른 PC에서는 접속할 수 없습니다.
- 생성 HTML의 보기 응답은 브라우저 sandbox에서 열립니다.
- 외부 CDN·API 연결은 CSP로 차단됩니다. v5 기본 HTML/SVG는 외부 리소스 없이 동작합니다.
- 요청 전체, HTML, 메타데이터, 저장소 전체에 각각 크기 제한이 있습니다.
- 리포트 ID는 서버가 만든 고정 형식만 허용하며, 저장 경로 밖으로 나가는 경로·symlink는 거부합니다.
- 링크는 기본 24시간, 최대 168시간 뒤 만료됩니다.
- 응답은 브라우저 캐시에 저장하지 않도록 `no-store` 헤더를 사용합니다.

랜덤 `report_id`는 인증 수단이 아닙니다. 기본값 `USE_ACCESS_TOKEN = False`에서는 URL을 아는 사람이 리포트를 열 수 있습니다.

## 2. 설정 위치

설정은 모두 [server.py](server.py) 상단의 `실행 설정` 영역에 보입니다.

```python
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8010
BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"
STORAGE_DIR = Path(__file__).resolve().parent / "storage"

DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 24 * 7
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 1 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_HTML_BYTES + MAX_METADATA_BYTES + (64 * 1024)
MAX_STORAGE_BYTES = 512 * 1024 * 1024

USE_ACCESS_TOKEN = False
ENABLE_ACCESS_LOG = False
```

| 설정 | 의미 |
| --- | --- |
| `SERVER_HOST` | Uvicorn이 실제로 대기할 주소 |
| `SERVER_PORT` | 기본 포트 `8010` |
| `BASE_URL` | API가 응답에 넣을 사용자 접속용 절대 주소 |
| `STORAGE_DIR` | 저장소 루트. 실제 파일은 그 아래 `reports`에 저장 |
| `DEFAULT_TTL_HOURS` | 요청에 TTL이 없을 때 적용할 시간 |
| `MAX_TTL_HOURS` | 클라이언트가 요청해도 넘을 수 없는 최대 TTL |
| `MAX_HTML_BYTES` | HTML UTF-8 byte 상한 |
| `MAX_METADATA_BYTES` | JSON 메타데이터 byte 상한 |
| `MAX_REQUEST_BYTES` | `POST /reports` 전체 body 상한 |
| `MAX_STORAGE_BYTES` | HTML과 JSON을 합친 저장소 논리 용량 상한 |
| `USE_ACCESS_TOKEN` | 새 링크에 접근 토큰을 붙일지 여부 |
| `ENABLE_ACCESS_LOG` | Uvicorn access log. query token 노출 방지를 위해 기본 비활성 |

크기 상수를 바꿀 때는 `MAX_REQUEST_BYTES > MAX_HTML_BYTES` 관계를 유지해야 합니다.

## 3. 처음 한 번 설치

PowerShell에서 실행합니다.

```powershell
cd C:\Users\<사용자명>\Desktop\metadata_driven_v5\report_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

가상환경 실행 정책 오류가 나면 현재 PowerShell 창에서만 정책을 완화한 뒤 다시 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 4. 실행과 상태 확인

```powershell
cd C:\Users\<사용자명>\Desktop\metadata_driven_v5\report_api
.\.venv\Scripts\Activate.ps1
python server.py
```

정상 로그 예시:

```text
HTML Report API: http://127.0.0.1:8010
Listen address: http://127.0.0.1:8010
Storage folder: ...\metadata_driven_v5\report_api\storage\reports
```

서버 PowerShell 창을 닫지 않은 상태에서 다음 주소를 확인합니다.

- 브라우저: `http://127.0.0.1:8010/` → `alive!`
- JSON 상태: `http://127.0.0.1:8010/health` → `{"status":"ok"}`
- API 문서: `http://127.0.0.1:8010/docs`

## 5. v5 Langflow 연결

`00 HTML 시각화 생성기`의 화면 입력을 다음처럼 둡니다.

| 입력 | 기본값 |
| --- | --- |
| `HTML Report API 주소` | `http://127.0.0.1:8010` |
| `HTML 링크 유효시간` | `24` |

컴포넌트는 주소 끝에 `/reports`가 없으면 자동으로 붙입니다. `http://127.0.0.1:8010`과 `http://127.0.0.1:8010/reports`를 모두 사용할 수 있지만, 문서와 운영 설정에서는 base URL 형식을 권장합니다.

서버가 꺼져 있거나 주소가 틀리면 로컬 HTML 파일은 생성될 수 있어도 공개 `view_url`·`download_url`은 발급되지 않습니다. 전체 연결 및 링크 전달 경계는 [HTML Report 링크 사용 가이드](../docs/HTML_REPORT_LINK_GUIDE.md)를 참고합니다.

## 6. API 요청·응답 계약

최소 요청:

```json
{
  "html": "<!doctype html><html><body><h1>테스트</h1></body></html>"
}
```

v5가 보내는 주요 필드:

```json
{
  "html": "<!doctype html>...",
  "title": "공정별 WIP",
  "question": "공정별 WIP을 시각화해줘",
  "view_request": "metadata_driven_v5 HTML/SVG chart",
  "available_datasets": [],
  "report_plan": {
    "source_flow": "10. v5_html_visualization",
    "chart_type": "bar",
    "x_column": "OPER_NAME",
    "y_columns": ["WIP"]
  },
  "ttl_hours": 24,
  "filename_hint": "공정별_WIP.html"
}
```

성공 시 HTTP `201`과 아래 6개 필드를 반환합니다.

```json
{
  "report_id": "20260719120000_0123456789abcdef0123456789abcdef",
  "title": "공정별 WIP",
  "view_url": "http://127.0.0.1:8010/reports/view/...",
  "download_url": "http://127.0.0.1:8010/reports/download/...",
  "expires_at": "2026-07-20T03:00:00+00:00",
  "ttl_hours": 24
}
```

`ttl_hours`가 `MAX_TTL_HOURS`보다 크면 최대값으로 낮춥니다. 1보다 작은 값은 요청 검증 단계에서 거절합니다.

## 7. PowerShell 단독 확인

서버가 실행 중인 새 PowerShell 창에서 다음을 실행합니다.

```powershell
$body = @{
  title = "로컬 테스트"
  question = "링크 발급 확인"
  html = "<!doctype html><html lang='ko'><body><h1>정상</h1></body></html>"
  ttl_hours = 1
  filename_hint = "로컬_테스트"
} | ConvertTo-Json -Depth 20

$created = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8010/reports" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body

$created | Format-List
Start-Process $created.view_url
Invoke-WebRequest -Uri $created.download_url -OutFile ".\report_test.html"
```

## 8. 저장·만료·용량 동작

리포트 하나는 다음 두 파일로 저장됩니다.

```text
storage\reports\<report_id>.html
storage\reports\<report_id>.json
```

- HTML과 JSON은 임시 파일에 먼저 기록한 뒤 최종 이름으로 교체합니다.
- 저장 중 실패하면 해당 리포트의 임시·부분 파일을 정리합니다.
- 서버 시작, 새 리포트 생성, 만료 링크 접근 시 만료·고아 파일을 정리합니다.
- 만료 링크는 `410 Gone`을 반환하고 파일을 삭제합니다.
- 새 리포트로 전체 용량을 넘기게 되면 오래된 리포트부터 삭제합니다. 따라서 TTL 전이라도 용량 압박으로 오래된 링크가 사라질 수 있습니다.
- `storage/`는 `.gitignore` 대상이며 영구 보관소가 아닙니다. 중요한 결과는 다운로드하거나 별도 승인 저장소로 옮깁니다.

서버는 프로세스 내부 lock을 사용하므로 `python server.py`의 단일 worker 실행을 전제로 합니다. 여러 worker/여러 서버 인스턴스가 같은 폴더를 공유해야 한다면 외부 파일 lock 또는 객체 저장소로 확장해야 합니다.

## 9. 다른 PC에서 열어야 할 때

로컬 기본값의 `127.0.0.1` 링크는 서버가 실행 중인 그 PC에서만 열립니다. 팀 공유가 필요하면 최소한 다음을 함께 바꿉니다.

```python
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8010
BASE_URL = "https://reports.example.internal"
USE_ACCESS_TOKEN = True
```

운영 전 확인 사항:

1. 내부 DNS 또는 고정 IP와 `BASE_URL`을 일치시킵니다.
2. 방화벽에서 필요한 사용자/망만 허용합니다.
3. 역방향 프록시에서 HTTPS를 적용합니다.
4. `USE_ACCESS_TOKEN = True`로 새 링크를 발급합니다.
5. query token이 브라우저 기록·프록시 로그·채팅 복사본에 남을 수 있으므로 URL 로그를 비활성화하거나 마스킹합니다.
6. 토큰 포함 URL을 LLM prompt, 분석 trace, 장기 로그에 넣지 않습니다.

`USE_ACCESS_TOKEN`을 켜도 기존 무토큰 파일이 자동으로 토큰 보호로 바뀌지는 않습니다. 설정 변경 시 `storage/reports`의 기존 파일을 정리하거나 별도 마이그레이션해야 합니다.

이 서버에는 사용자 계정, 권한 그룹, 감사 로그, 악성 HTML 정적 분석 기능이 없습니다. 조직 공용 서비스로 승격할 때는 역방향 프록시 인증, 사용자별 권한, HTTPS, 감사·삭제 정책을 추가해야 합니다.

## 10. 문제 해결

### 포트 충돌

`SERVER_PORT`와 `BASE_URL`의 포트를 함께 바꾸고 Langflow의 `HTML Report API 주소`도 맞춥니다.

### 링크는 생성되지만 다른 PC에서 열리지 않음

`127.0.0.1`은 각 PC 자신을 뜻합니다. 서버의 실제 DNS/IP를 `BASE_URL`에 사용하고 방화벽·HTTPS 설정을 확인합니다.

### `413 Request Entity Too Large`

HTML/메타데이터/전체 요청 중 하나가 상한을 넘었습니다. 원본 데이터를 HTML에 모두 넣기보다 표시 행을 줄이고, 꼭 필요할 때만 관련 상수를 함께 조정합니다.

### `410 Gone`

TTL이 지났습니다. Langflow에서 시각화를 다시 생성해 새 링크를 발급합니다.

### `403 invalid access token`

토큰 보호 리포트인데 URL의 `token`이 없거나 달라졌습니다. 링크를 중간에서 잘라 복사하지 않았는지 확인합니다.

### 외부 JS/CSS/CDN이 로드되지 않음

기본 CSP가 의도적으로 외부 연결을 차단합니다. v5처럼 CSS/SVG/스크립트를 HTML 안에 포함하는 방식을 권장합니다. 정책 완화가 필요하면 `VIEW_CONTENT_SECURITY_POLICY`에 승인된 도메인만 추가합니다.
