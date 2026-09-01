# PTMORE PKG Agent Portal

이 포털은 메타데이터 등록을 외부 rev_2 Flow API로 실행하고, 대시보드는 Phoenix의 실제 사용 이력을 조회합니다.

- 대시보드 사용 이력은 최근 3주 Phoenix/MongoDB 보관 이력만 표시합니다.
- 로그인 사용자는 운영 HCP SSO 또는 로컬 고정 사용자로 구분합니다. 운영 권한은 사번을 기준으로 Portal MongoDB 설정에서 확인하며, 로컬 고정 사용자는 개발 편의를 위해 관리자입니다.
- 메타데이터 등록 요청은 포털 서버가 외부 API로 전달합니다. 브라우저에는 API 키나 MongoDB URI가 내려가지 않습니다.
- 스케줄 등록 정보는 Portal MongoDB에 저장하고, 별도 Scheduler Worker가 GAIA 실행과 CUBE 개인 DM 발송을 처리합니다.

## 동작 흐름

```text
브라우저 등록 화면
  -> 포털 POST /api/metadata-authoring
  -> 포털 서버 환경설정 + 관리자 설정 저장값
  -> 외부 rev_2 Flow API
  -> 10 ... API 응답 생성기의 구조화 결과
  -> 포털 결과 화면
```

포털은 Chat Output 문자열을 결과로 추측하지 않습니다. `Api-...-rev-2` 터미널의 `api_response` 구조를 찾아 후보 표, 계약 검증, 중복 처리 계획, 저장 결과를 그대로 표시합니다.

## 처음 실행하기

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\ptmore_portal
python -m pip install -r requirements.txt
Copy-Item .env.example .env

# 로컬 테스트: HCP SSO 없이 고정 사용자(2011111 / 문봉건)로 실행합니다.
python -m uvicorn app_local:application --host 127.0.0.1 --port 8002
```

브라우저에서 `http://127.0.0.1:8002`를 엽니다.

운영 서버에서는 아래 고정 진입점을 사용합니다. 이 경우 포트는 `5000`입니다. 실행 전에 HCP Secret에 `PTMORE_SSO_SESSION_SECRET`을 입력하고, `.env`의 `PTMORE_PORTAL_AUTH_MODE=production`을 유지합니다.

```powershell
python app.py
```

초기 `.env.example`은 실제 연동 모드 기준입니다. 예시 주소·키는 운영 값으로 교체해야 하며, 설정이 없으면 가짜 성공 결과 대신 명확한 연결 오류를 표시합니다.

## 사번·이름 로그인 방식

- 운영 `app.py`: HCP에서 제공하는 `hcputil.auth.sso.SSO`로 SSO 쿠키를 확인합니다. 성공하면 `emp_no`, `emp_name`만 서명된 Portal 세션에 저장합니다.
- 로컬 `app_local.py`: HCP SSO 모듈을 불러오지 않으며 항상 `2011111 / 문봉건` 로컬 관리자로 실행합니다.
- 브라우저가 보내는 `X-PTMORE-Employee-Id`, `X-PTMORE-Employee-Name` 헤더는 운영·로컬 모두 사용자 식별에 사용하지 않습니다.

운영 환경에는 아래 두 값이 필요합니다. 세션 Secret은 충분히 긴 임의 문자열로 만들고 HCP Secret으로 관리합니다.

```dotenv
PTMORE_PORTAL_AUTH_MODE=production
PTMORE_SSO_SESSION_SECRET=<HCP-Secret-세션-서명값>
PTMORE_SSO_SESSION_HTTPS_ONLY=true
```

실제 SSO 모듈과 로그인 리다이렉트는 HCP 운영 환경에서만 확인할 수 있습니다. 로컬 PC에서는 `app_local.py`로만 화면을 확인하세요.

## Phoenix 실제 사용 이력 연결

대시보드는 Phoenix 실제 이력만 사용합니다. `.env`에 아래 값을 입력하며 API Key는 브라우저로 전달되지 않고 Portal 서버에서만 사용됩니다.

```dotenv
PTMORE_USAGE_HISTORY_MODE=phoenix

PTMORE_PHOENIX_ENDPOINT=https://<phoenix-host>
PTMORE_PHOENIX_API_KEY=<phoenix-api-key>

# 프로젝트 이름 또는 GraphQL Project ID를 여러 개 입력할 수 있습니다.
PTMORE_PHOENIX_PROJECTS_JSON=["<project-name-or-id-1>", "<project-name-or-id-2>"]
```

선택 설정은 다음과 같습니다.

```dotenv
PTMORE_PHOENIX_TIMEOUT_SECONDS=30
PTMORE_PHOENIX_PAGE_SIZE=500
PTMORE_PHOENIX_FILTER_CONDITION=span_kind == 'CHAIN'
PTMORE_PHOENIX_SPAN_NAME_PREFIX=GaiA Input
```

`GET /api/dashboard/usage`는 최근 **21일(KST)** 범위에서 `GaiA Input`으로 시작하는 span을 찾고, 하나의 trace를 하나의 질문으로 집계합니다. `phoenix + configured` 운영 모드에서는 MongoDB 보관 이력을 먼저 읽고, 당일과 아직 보관되지 않은 날짜만 Phoenix에서 보완 조회합니다. 각 기록에서는 다음 정보만 Portal에 전달합니다.

- `query_time`: 질문 시각(KST)
- `platform`: 유입 경로
- `user_id`: 사용자 사번
- `question`: 질문 원문

Phoenix 설정이 비어 있거나 API 권한·네트워크 오류가 발생하면 Portal은 `503`으로 명확히 실패를 알립니다. 이때 실제 조회 실패를 더미 이력으로 바꿔 보여주지 않습니다. 설정을 바꾼 뒤에는 Portal 서버를 재시작하세요.

### Phoenix 사용 이력 보관과 새로고침

Phoenix는 최근 3주만 보관하므로, 실제 사용 이력은 Portal MongoDB 보관소에 함께 저장합니다. 사용 이력 보관만을 위한 HCP Cron, 별도 Worker, 서버 간 동기화 API/Secret은 필요하지 않습니다.

```dotenv
PTMORE_USAGE_HISTORY_MODE=phoenix
PTMORE_USAGE_HISTORY_ARCHIVE_MODE=configured
PTMORE_USAGE_HISTORY_COLLECTION=portal_usage_history
```

일반 대시보드 요청(`GET /api/dashboard/usage`)은 MongoDB의 최근 21일 보관 이력을 먼저 읽고, 아래 범위만 Phoenix에서 다시 조회해 보관소를 갱신합니다.

- 당일(KST)은 접속할 때마다 갱신합니다.
- 과거 날짜는 프로젝트별 정상 조회 완료 이력이 없을 때만 보완 조회합니다. 질문이 0건인 날도 완료로 기록되므로 계속 재조회하지 않습니다.
- 처음 대시보드를 열 때는 보관 이력이 없으므로 최근 21일 전체를 조회해 초기화합니다.

활성 관리자는 화면의 `최근 3주 전체 새로고침`으로 `POST /api/dashboard/usage/refresh`를 호출해 최근 21일 전체를 Phoenix 기준으로 다시 갱신할 수 있습니다. 일반 사용자는 이 작업을 실행할 수 없으며, 서버도 관리자 권한을 확인합니다.

대시보드 활동이 21일을 넘게 없으면 그 사이 Phoenix에서 사라진 기록은 나중에 소급 복구할 수 없습니다. 최근 3주 이력이 필요할 때는 21일 안에 대시보드에 한 번 이상 접속하세요.

## 실제 메타데이터 API 연결

`.env`에서 다음 항목을 채웁니다. 실제 `.env` 파일은 Git에 올리지 않습니다.

```dotenv
PTMORE_METADATA_API_MODE=api

# 세 rev_2 Flow가 같은 실행 API를 쓸 때 하나만 설정합니다.
PTMORE_METADATA_API_URL=https://<metadata-flow-api-url>

# API 인증 방식에 맞춰 설정합니다.
PTMORE_METADATA_API_AUTH_HEADER=X-Gaia-Auth-Key
PTMORE_METADATA_API_AUTH_KEY=<api-key>

# 구조화된 `10 ... API 응답 생성기` 결과를 받기 위해 유지합니다.
PTMORE_METADATA_API_OUTPUT_TYPE=any

# 포털이 신뢰된 내부 Flow API에 MongoDB 연결 값을 tweak으로 전달해야 할 때만 true입니다.
PTMORE_METADATA_SEND_MONGODB_TWEAKS=true
MONGODB_URI=mongodb://<host>:<port>
MONGODB_DATABASE=datagov
MONGODB_COLLECTION_PREFIX=agent_v4_
```

세 Flow가 서로 다른 실행 주소를 사용한다면 공통 `PTMORE_METADATA_API_URL` 대신 아래 세 값만 채웁니다.

```dotenv
PTMORE_METADATA_TABLE_CATALOG_API_URL=https://<table-catalog-flow-api-url>
PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL=https://<main-filter-flow-api-url>
PTMORE_METADATA_DOMAIN_API_URL=https://<domain-flow-api-url>
```

전체 선택 항목과 기본값은 [.env.example](.env.example)에 있습니다.

### GAIA API 호출 권한 사번

`GAIA API 호출 권한 사번`은 `.env`에 넣지 않습니다. 관리자만 설정 화면에서 변경할 수 있으며, 포털은 `MONGODB_URI`와 `MONGODB_DATABASE`가 가리키는 DB의 고정 `portal_settings` 컬렉션에 이 값을 저장합니다. 이후 GAIA API 호출 시 서버가 이 값을 읽어 `X-Gaia-User-Id` 헤더에 넣습니다.

API Key는 계속 서버의 `.env` 또는 Secret 관리 도구에서만 관리합니다. 브라우저 설정 화면에는 표시하거나 저장하지 않습니다.

### MongoDB 값 전달 기준

`MONGODB_URI`와 `MONGODB_DATABASE`는 관리자 설정 저장에도 사용합니다. 두 값이 없으면 설정 변경은 저장되지 않으므로 운영 환경에서는 반드시 입력합니다.

### 포털·스케줄 컬렉션 이름

같은 MongoDB 데이터베이스에서 포털이 사용할 컬렉션 이름은 `.env`에서 아래처럼 정합니다.

```dotenv
PTMORE_PORTAL_SETTINGS_COLLECTION=portal_settings
PTMORE_PORTAL_AUDIT_COLLECTION=portal_audit_log
PTMORE_SCHEDULE_COLLECTION=portal_schedules
PTMORE_SCHEDULE_RUN_COLLECTION=portal_schedule_runs
```

- `PTMORE_PORTAL_SETTINGS_COLLECTION`: 관리자 설정
- `PTMORE_PORTAL_AUDIT_COLLECTION`: 관리자 설정 변경 이력
- `PTMORE_SCHEDULE_COLLECTION`: Portal이 등록·수정·활성화·삭제하는 스케줄 원본
- `PTMORE_SCHEDULE_RUN_COLLECTION`: Worker가 남기는 실행 성공·실패 이력

`MONGODB_URI`, `MONGODB_DATABASE`와 위 두 컬렉션을 설정하면 스케줄 화면은 더미를 사용하지 않고 실제 MongoDB를 조회합니다. 모든 로그인 사용자는 전체 목록을 볼 수 있지만, 수정·활성화/일시중지·삭제는 등록자 본인 또는 활성 관리자만 할 수 있습니다. MongoDB를 설정하지 않았거나 연결하지 못하면 스케줄 API는 더미로 대체하지 않고 `503`을 반환합니다.

### Scheduler Worker 실행

Portal 웹 서버는 스케줄을 **저장만** 합니다. 실제 정기 실행은 callback 서버와 독립 배포되는 Worker가 담당합니다. Portal과 Worker는 같은 MongoDB의 위 두 컬렉션 이름을 사용해야 합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\scheduler_worker_server
python -m pip install -r requirements.txt
python app.py
```

Worker는 due 상태의 활성 스케줄을 원자적으로 선점한 뒤, 등록자 개인 DM으로만 결과를 보냅니다. 일반 채팅의 처리중 안내 문구는 보내지 않으며, 매 실행마다 `cube_scheduling_<사번>_<UUID>` 세션과 `platform=CUBE_SCHEDULING`을 사용합니다. Worker는 자체 `app.py`를 Uvicorn으로 실행하며 `/health`, `/ready`로 HCP 상태를 확인합니다. 운영 환경에서는 Portal, CUBE callback `app.py`, Scheduler Worker를 각각 독립적으로 재시작 가능한 프로세스/서비스로 운영하세요. Worker는 callback `app.py`를 import하지 않으며 필요한 GAIA·CUBE·Markdown 변환 코드를 자체 포함합니다. 자세한 환경 변수와 일회성 점검 방법은 [독립 Scheduler Worker 안내](../gaia_cube_server/scheduler_worker_server/README.md)를 참조하세요.

`PTMORE_METADATA_SEND_MONGODB_TWEAKS=true`이면 포털은 신뢰된 내부 API 요청에만 다음 rev_2 노드 값을 `tweaks`로 전달합니다.

- `01 메타데이터 QA 통합 Snapshot 로더`: 기존 메타데이터·중복 후보를 읽기 위한 URI, DB, 세 컬렉션 이름
- `07 ... 검수/저장 처리기`: 실제 저장 대상 URI, DB, 컬렉션 이름

외부 경유 API처럼 요청 본문에 MongoDB URI가 남으면 안 되는 환경에서는 이 값을 `false`로 둡니다. 이 경우 MongoDB 설정은 원격 Flow 런타임 환경변수에서 관리해야 합니다.

## API 계약

포털 화면은 아래 요청만 포털 서버로 보냅니다.

```json
{
  "metadata_type": "table_catalog",
  "raw_text": "등록할 자연어 원문",
  "duplicate_action": "skip",
  "dry_run": true
}
```

포털 서버는 Langflow 실행 API 형식으로 변환합니다.

```json
{
  "input_value": "등록할 자연어 원문",
  "input_type": "chat",
  "output_type": "any",
  "tweaks": {
    "Request-table_catalog-rev-2": {
      "duplicate_action": "skip",
      "dry_run": true
    }
  }
}
```

MongoDB tweak 사용을 명시적으로 켠 경우에만 Snapshot/Writer 노드 값이 위 `tweaks`에 추가됩니다. API 응답은 포털 내부에서 아래 형태로 정리되어 화면에 전달됩니다.

```json
{
  "run_id": "META-...",
  "requested_at": "2026-...",
  "metadata_type": "table_catalog",
  "preview_only": false,
  "requested_dry_run": true,
  "response": { "response_type": "metadata_authoring" }
}
```

`GET /api/metadata-authoring/status`는 활성 관리자에게만 연결 준비 상태를 반환하며, API 키나 MongoDB URI는 반환하지 않습니다.

## rev_2 Flow에 대한 영향

Flow JSON이나 Custom Component는 변경하지 않았습니다. 기존 Canvas 실행과 기본값은 동일합니다.

포털은 API 호출할 때만 Langflow의 `tweaks`를 이용해 요청 로더의 `duplicate_action`, `dry_run` 및 필요 시 MongoDB 입력을 덮어씁니다. 기본값은 rev_2의 고유한 한국어 노드 표시명을 사용하므로 재-import로 ID가 바뀌어도 수정할 필요가 없습니다. 운영자가 표시명까지 바꾼 경우에만 `PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON`에 새 표시명 또는 node ID를 지정하면 됩니다.

`03. v5_table_catalog_saving_rev_2`는 Snapshot Loader 없이 기존 Writer 경로를 재사용합니다. `PTMORE_METADATA_SEND_MONGODB_TWEAKS=true`인 환경에서는 Portal이 없는 노드에 tweak를 보내지 않도록 Table Catalog의 `snapshot_loader`를 비워 둡니다. 현재 기본값도 비어 있으며, 운영 설정을 명시할 때는 아래처럼 입력합니다.

```dotenv
PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON={"table_catalog":{"snapshot_loader":""}}
```

`null`도 같은 의미입니다. 키를 아예 생략하면 기존 기본값을 유지하므로, Flow variant별 전환은 환경 설정으로만 조정할 수 있습니다. 이 설정은 Writer와 API terminal의 ID/표시명, 저장 정책, Portal 응답 계약을 바꾸지 않습니다.

## 화면 확인

- 로컬 화면: `http://127.0.0.1:8002`
- 운영 화면: 배포된 HCP Portal URL

로컬 고정 사용자 `2011111 / 문봉건`은 `app_local.py`에서만 활성 관리자로 처리됩니다. 이 권한은 MongoDB에 저장되지 않으며, 운영 HCP SSO 사용자의 관리자 권한에는 영향을 주지 않습니다.

## 관리자 권한과 설정 저장

- 메타데이터 등록, 메타데이터 API 상태 확인, 관리자 설정 API는 서버에서도 활성 관리자 사번을 확인합니다. 일반 사용자가 브라우저 주소나 API를 직접 호출해도 `403`으로 차단됩니다.
- 설정 화면의 `GAIA API 호출 권한 사번`과 활성 사용자 기준은 MongoDB의 고정 `portal_settings` 컬렉션에 저장됩니다. 변경 시점·변경 관리자·변경 전후 값은 `portal_audit_log`에 기록됩니다.
- GAIA API Key, Bearer Token, MongoDB URI와 같은 비밀값은 계속 `.env` 또는 Secret 관리 도구에서만 관리합니다. 관리자 설정 API는 이 값들을 반환하지 않습니다.

운영 Portal은 SSO 로그인 뒤 서버 세션에 저장된 사번·이름만 사용합니다. 따라서 브라우저 개발자 도구로 임의 사번 헤더를 추가해도 관리자 권한을 얻을 수 없습니다.
