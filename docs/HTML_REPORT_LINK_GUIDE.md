# HTML Report 링크 사용 가이드

이 문서는 `metadata_driven_v5`의 HTML 시각화 결과를 로컬 파일 경로와 웹 보기·다운로드 링크로 전달하는 방법을 설명합니다.

## 1. 전체 흐름

```text
Data Analysis 결과의 result_ref
  -> 00 HTML 시각화 생성기
     -> Langflow 저장소에 HTML 파일 저장(path)
     -> Report API의 POST /reports 호출
        -> report_api/storage/reports에 HTML + JSON 저장
        -> view_url + download_url + expires_at 반환
  -> artifact descriptor에 path와 검증된 URL을 분리 보존
  -> 최종 사용자 메시지에 보기/다운로드 Markdown 링크 표시
```

두 저장 위치의 역할은 다릅니다.

| 값 | 역할 |
| --- | --- |
| `path` | Langflow 런타임이 소유한 로컬 파일. `Message.files` 첨부에 사용 |
| `view_url` | 브라우저에서 HTML을 여는 절대 `http(s)` URL |
| `download_url` | HTML 파일을 내려받는 절대 `http(s)` URL |
| `expires_at` | Report API 링크 만료 UTC 시각 |
| `report_id` | Report API 저장 파일 쌍의 식별자 |

URL을 로컬 `path`처럼 `Message.files`에 넣지 않습니다. 반대로 로컬 `path`를 Markdown 웹 링크로 노출하지 않습니다.

## 2. 서버 준비

먼저 [report_api README](../report_api/README.md)에 따라 설치하고 서버를 실행합니다.

```powershell
cd C:\Users\<사용자명>\Desktop\metadata_driven_v5\report_api
.\.venv\Scripts\Activate.ps1
python server.py
```

브라우저에서 `http://127.0.0.1:8010/`을 열어 `alive!`를 확인합니다.

## 3. Langflow 입력

`00 HTML 시각화 생성기`에서 다음 두 입력을 확인합니다.

| 화면 입력 | 권장 시작값 | 설명 |
| --- | --- | --- |
| `HTML Report API 주소` | `http://127.0.0.1:8010` | `/reports`는 컴포넌트가 자동 보완 |
| `HTML 링크 유효시간` | `24` | 서버가 1~168시간 범위로 제한 |

주소는 절대 `http://` 또는 `https://` URL이어야 합니다. 사용자 정보가 포함된 URL, 상대경로, `file://`, `javascript:`는 사용하지 않습니다.

## 4. 사용자에게 보이는 결과

링크 발급에 성공하면 사용자 메시지에는 다음과 같은 결정적 Markdown을 사용합니다.

```markdown
[HTML 차트 보기](https://.../reports/view/<report_id>) · [HTML 다운로드](https://.../reports/download/<report_id>)
```

모델에게 URL 문구를 다시 작성시키지 않고, 검증된 artifact의 URL을 최종 단계에서 붙이는 방식을 권장합니다. 그래야 query token이 바뀌거나 링크가 잘리는 문제를 줄일 수 있습니다.

링크 발급에 실패한 경우:

- 로컬 `path`가 있으면 Langflow 파일 첨부는 유지할 수 있습니다.
- `view_url`·`download_url`을 빈 값으로 명확히 처리합니다.
- 없는 URL을 추측해서 만들거나 `127.0.0.1` 링크를 임의로 합성하지 않습니다.
- 사용자에게 Report API 주소와 실행 상태를 확인하라는 안내를 표시합니다.

## 5. Workflow 경계 규칙

Route V4 같은 상위 Workflow가 artifact를 전달할 때 다음 원칙을 지킵니다.

1. `view_url`과 `download_url`은 길이 제한을 적용한 절대 `http(s)` URL만 보존합니다.
2. URL의 username/password, fragment, 제어문자를 거부합니다.
3. 링크는 top-level artifact descriptor에서 단계 간 전달합니다.
4. LLM용 `workflow_context`, `step summary`, prompt에는 URL과 query token을 넣지 않습니다.
5. 최종 응답 조립기가 artifact의 검증된 링크를 결정적으로 붙입니다.
6. 원본 HTML과 Report API의 raw 응답은 Workflow payload·trace에 남기지 않습니다.
7. `Message.files`에는 실제 로컬 `path`가 있을 때만 넣습니다.

권장 artifact 예시:

```json
{
  "artifact_type": "html_chart",
  "path": "<Langflow가 저장한 로컬 파일 경로>",
  "mime_type": "text/html",
  "title": "공정별 WIP",
  "download_name": "공정별_WIP.html",
  "report_id": "20260719120000_0123456789abcdef0123456789abcdef",
  "view_url": "https://reports.example.internal/reports/view/...",
  "download_url": "https://reports.example.internal/reports/download/...",
  "expires_at": "2026-07-20T03:00:00+00:00",
  "ttl_hours": 24
}
```

LLM에 제공할 짧은 summary에는 예를 들어 `HTML 시각화 생성 완료: bar, OPER_NAME 대비 WIP`처럼 링크가 없는 설명만 사용합니다.

## 6. 로컬 사용과 팀 공유의 차이

### 같은 PC에서 사용

- 서버: `SERVER_HOST = "127.0.0.1"`
- 링크: `BASE_URL = "http://127.0.0.1:8010"`
- Langflow: `HTML Report API 주소 = http://127.0.0.1:8010`

서버가 실행 중인 같은 PC에서만 링크가 열립니다.

### 내부 사용자와 공유

- 서버는 내부 호스트에서 실행합니다.
- `SERVER_HOST = "0.0.0.0"`으로 수신하되 방화벽으로 허용 범위를 제한합니다.
- `BASE_URL`에는 사용자가 실제 접속하는 HTTPS DNS를 넣습니다.
- Langflow 입력도 그 HTTPS base URL과 맞춥니다.
- `USE_ACCESS_TOKEN = True`, 역방향 프록시 인증, HTTPS를 함께 적용합니다.

`0.0.0.0`은 수신 주소이지 사용자가 클릭할 주소가 아닙니다. `BASE_URL = "http://0.0.0.0:8010"`으로 설정하지 않습니다.

## 7. 토큰과 민감정보

토큰 모드에서는 보기·다운로드 URL에 `?token=...`이 붙습니다. 서버 파일에는 원문 대신 SHA-256 해시만 저장하지만, URL 자체는 다음 위치에 남을 수 있습니다.

- 브라우저 방문 기록
- 프록시 또는 웹 서버 access log
- 복사한 채팅 메시지
- 스크린샷과 사용자 메모

따라서 다음을 지킵니다.

- Uvicorn access log는 기본 비활성 상태로 둡니다.
- proxy 로그에서는 `token` query를 마스킹합니다.
- token URL을 LLM prompt, 메타데이터 검색 인덱스, 장기 trace에 넣지 않습니다.
- 링크를 받은 사용자는 비밀번호처럼 취급하고 재공유하지 않습니다.
- 더 높은 보증이 필요하면 query token 대신 프록시 세션 인증과 사용자별 권한을 사용합니다.

생성 HTML에도 API key, DB 비밀번호, 세션 토큰, 원본 대용량 행 데이터를 넣지 않습니다.

## 8. 점검 절차

서버 실행 후 다음 순서로 확인합니다.

1. `/`과 `/health`가 정상인지 확인합니다.
2. `POST /reports` 단독 테스트에서 HTTP `201`과 6개 응답 필드를 확인합니다.
3. `view_url` 응답이 `text/html`이고 차트가 보이는지 확인합니다.
4. `download_url`에 `Content-Disposition: attachment`가 있는지 확인합니다.
5. TTL을 짧게 둔 테스트 또는 메타데이터 만료 시뮬레이션에서 `410 Gone`을 확인합니다.
6. `../` 형태 report ID, 빈 HTML, 최대 크기 초과 요청이 거절되는지 확인합니다.
7. Workflow API payload에는 원본 HTML이 없고 작은 artifact descriptor만 있는지 확인합니다.
8. LLM prompt용 context에 `view_url`, `download_url`, `?token=`이 없는지 확인합니다.
9. 최종 사용자 메시지에는 artifact에서 가져온 링크가 정확히 한 번만 표시되는지 확인합니다.

## 9. 자주 생기는 문제

| 증상 | 확인할 내용 |
| --- | --- |
| `Report API 연결 실패` | 서버 창, 포트, `HTML Report API 주소` |
| 링크가 다른 PC에서 안 열림 | `127.0.0.1` 대신 실제 HTTPS DNS를 `BASE_URL`에 사용했는지 |
| `403` | token query가 잘리거나 다른 링크의 token과 섞이지 않았는지 |
| `404` | 저장소 용량 제한으로 오래된 리포트가 먼저 삭제됐는지 |
| `410` | TTL이 지나 링크와 파일이 만료됐는지 |
| `413` | HTML/메타데이터/요청 body 상한을 넘었는지 |
| 차트 HTML에서 외부 리소스 오류 | 기본 CSP가 CDN/API를 차단함. self-contained HTML인지 확인 |

Report API는 링크 전달을 위한 지원 서버이며 영구 문서 관리 시스템은 아닙니다. 장기 보존·사용자별 권한·감사가 필요하면 승인된 내부 저장소와 인증 계층을 별도로 연결합니다.
