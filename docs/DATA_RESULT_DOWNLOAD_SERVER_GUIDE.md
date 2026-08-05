# Data Analysis 결과 직접 다운로드 서버 가이드

## 1. 목적

Data Analysis Flow의 `23 MongoDB 결과 저장소`가 발급한 `data_ref`를 CSV로 내려받고, Realtime Production Report가 만든 HTML을 같은 서버에서 등록·보기·다운로드합니다. 기존 HTML Visualization Flow도 `HTML Report API 주소`를 이 서버로 명시적으로 바꾸면 같은 API를 선택적으로 사용할 수 있습니다.

- pandas 최종 분석 결과
- 분석에 사용한 데이터셋별 원본 행
- 실시간 생산 분석 Report

링크는 중간 웹 화면을 열지 않습니다. GaiA 답변 본문의 링크는 `target="_blank"`로 다운로드 요청을 새 탭에 분리하고, 서버는 MongoDB에서 해당 경로를 읽어 `Content-Disposition: attachment`로 CSV를 반환합니다. 따라서 Langflow Playground의 현재 탭을 벗어날 때 나타나는 편집 내용 보호 팝업을 피할 수 있습니다.

## 2. 구성

```text
23 MongoDB 결과 저장소
  -> MongoDB 문서 저장(expires_at, 기본 1시간)
  -> data_ref별 /download.csv URL 발급
  -> 21 답변 메시지 어댑터
  -> GaiA answer Markdown + metadata.urls
  -> 사용자 클릭
  -> artifact_server (FastAPI)
  -> CSV 직접 다운로드
```

MongoDB TTL 인덱스는 `expires_at` 필드를 기준으로 문서를 정리합니다. TTL 삭제 주기는 MongoDB 내부 스케줄에 따라 약간 늦을 수 있으나, 다운로드 서버는 요청 시점에도 만료 시간을 검사해 만료된 링크에 `410 Gone`을 반환합니다.

## 3. 서버 실행

서버는 프로젝트 루트의 `.env`에서 MongoDB 연결값을 읽습니다.

```env
MONGODB_URI=mongodb://user:password@host:27017
MONGODB_DATABASE=datagov
MONGODB_RESULT_COLLECTION=agent_v4_result_store
ARTIFACT_LISTEN_HOST=0.0.0.0
ARTIFACT_LISTEN_PORT=8765
ARTIFACT_PUBLIC_BASE_URL=https://artifact.example.internal
DATA_REF_DOWNLOAD_MAX_BYTES=67108864
REPORT_STORAGE_DIR=C:\Users\<사용자명>\Desktop\metadata_driven_v5\report_api\storage
REPORT_DEFAULT_TTL_HOURS=24
REPORT_MAX_TTL_HOURS=168
REPORT_MAX_HTML_BYTES=10485760
REPORT_MAX_STORAGE_BYTES=536870912
REPORT_USE_ACCESS_TOKEN=false
```

PowerShell 실행 예시:

```powershell
cd C:\Users\<사용자명>\Desktop\metadata_driven_v5
python -m artifact_server
```

상태 확인:

```text
http://127.0.0.1:8765/health
```

정상 응답의 `features`에는 `data_ref_csv=true`, `html_reports=true`가 함께 표시됩니다.

### 개발용 기존 launcher

`tools/data_ref_download_server.py`는 기존 route 검증과 로컬 포트 교체가 필요한 개발 환경을 위해 유지합니다. 운영 프로세스는 FastAPI 앱을 supervisor/Kubernetes/Windows Service가 관리하도록 구성합니다.

```powershell
python tools\data_ref_download_server.py --host 0.0.0.0 --port 8765
```

- FastAPI 운영 앱에는 포트 listener 강제 종료 기능이 없습니다.
- 같은 포트 재배치와 비정상 종료 복구는 process supervisor가 담당해야 합니다.
- 기존 launcher의 `--force-replace-port`는 지정 포트를 이 서비스만 쓰는 로컬 개발 환경에서만 사용합니다.

## 4. Langflow 설정

`23 MongoDB 결과 저장소`에서 다음 값을 설정합니다.

| 입력 | 로컬 예시 | Kubernetes 예시 |
| --- | --- | --- |
| 다운로드 링크 Base URL | `http://127.0.0.1:8765` | `https://data-download.example.internal` |
| HTML Report API 주소 | `http://127.0.0.1:8765` | Langflow가 접근할 수 있는 통합 서버 Service 주소 |
| 데이터 보관 시간(시간) | `1` | `1` |

Kubernetes에서 `127.0.0.1`, `localhost`, `0.0.0.0`은 사용자 브라우저가 접근할 주소가 아닙니다. Service/Ingress의 내부 HTTPS 주소를 Base URL로 넣어야 합니다.

21번에는 Base URL 입력이 없습니다. 21번은 23번이 발급한 URL만 표시합니다.

답변 본문 링크의 새 탭 속성은 `21 답변 메시지 어댑터`가 적용합니다. 다운로드 서버의 응답 헤더만으로는 링크를 누르기 전에 브라우저가 발생시키는 `beforeunload` 경고를 제어할 수 없습니다. GaiA의 별도 출처 패널에 표시되는 `metadata.urls` 링크의 탭 전환 정책은 GaiA 웹 UI가 결정합니다.

## 5. 직접 다운로드 계약

정상 링크 형식:

```text
GET /download.csv?download_ref=<URL-safe-token>
```

정상 응답 주요 헤더:

```text
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="...csv"; filename*=UTF-8''...
Cache-Control: no-store, max-age=0
X-Content-Type-Options: nosniff
```

CSV는 Excel에서 한글을 인식할 수 있도록 UTF-8 BOM을 포함합니다.

HTML Report 계약:

```text
POST /reports
GET /reports/view/<report_id>
GET /reports/download/<report_id>
DELETE /reports/<report_id>
```

`POST /reports`는 `html`, `title`, `question`, `ttl_hours`, `filename_hint` JSON을 받고 `report_id`, `view_url`, `download_url`, `expires_at`, `ttl_hours`를 반환합니다. 11 Realtime Production Report Flow의 기본 `HTML Report API 주소`는 `http://127.0.0.1:8765`입니다.

## 6. 보호 규칙

- 서버 설정과 다른 MongoDB database/collection은 거부합니다.
- `payload.result_rows`와 `payload.runtime_sources.<alias>` 경로만 허용합니다.
- 23번이 발급하는 `result:<session>:<uuid>` 형식만 허용합니다.
- 만료된 결과는 `410 Gone`을 반환합니다.
- 다운로드 byte 상한을 넘으면 `413 Request Entity Too Large`를 반환합니다.
- 응답은 브라우저 캐시에 저장하지 않습니다.

운영 배포에서는 HTTPS, 내부 인증 프록시, 접근 로그의 query token 마스킹을 추가해야 합니다. Base URL이나 MongoDB URI에 사용자명·비밀번호를 포함한 HTTP URL은 사용하지 않습니다.
