# PTMORE PKG Agent Portal

이 포털은 메타데이터 등록을 외부 rev_2 Flow API로 실행할 수 있으며, 대시보드는 선택적으로 Phoenix의 실제 사용 이력을 조회할 수 있습니다.

- 대시보드 사용 이력은 기본적으로 더미 미리보기이며, Phoenix 모드를 명시적으로 켜면 최근 3주 실제 이력을 조회합니다.
- 사번/권한 미리보기와 스케줄 화면은 현재 더미 로직을 사용합니다.
- 메타데이터 등록 요청은 포털 서버가 외부 API로 전달합니다. 브라우저에는 API 키나 MongoDB URI가 내려가지 않습니다.
- CUBE callback 서버와는 별도입니다. 이 포털은 CUBE 메시지를 직접 수신하거나 발송하지 않습니다.

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

# 로컬 테스트: 운영 진입점은 바꾸지 않고 포트만 8002로 사용합니다.
python -m uvicorn app:application --host 127.0.0.1 --port 8002
```

브라우저에서 `http://127.0.0.1:8002`를 엽니다.

운영 서버에서는 아래 고정 진입점을 사용합니다. 이 경우 포트는 `5000`입니다.

```powershell
python app.py
```

초기 `.env`는 `PTMORE_METADATA_API_MODE=preview`이므로 안전한 미리보기 결과만 보여 줍니다. 외부 API나 MongoDB에는 연결하지 않습니다.

## Phoenix 실제 사용 이력 연결

대시보드는 기본적으로 `preview` 모드입니다. 실제 Phoenix 이력을 조회하려면 `.env`에 아래 값을 입력합니다. API Key는 브라우저로 전달되지 않고 Portal 서버에서만 사용됩니다.

```dotenv
# preview 대신 phoenix로 변경
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

`GET /api/dashboard/usage`는 요청 시점에 Phoenix를 조회합니다. 최근 **21일(KST)** 범위에서 `GaiA Input`으로 시작하는 span을 찾고, 하나의 trace를 하나의 질문으로 집계합니다. 각 기록에서는 다음 정보만 Portal에 전달합니다.

- `query_time`: 질문 시각(KST)
- `platform`: 유입 경로
- `user_id`: 사용자 사번
- `question`: 질문 원문

Phoenix 설정이 비어 있거나 API 권한·네트워크 오류가 발생하면 Portal은 `503`으로 명확히 실패를 알립니다. 이때 실제 조회 실패를 더미 이력으로 바꿔 보여주지 않습니다. 설정을 바꾼 뒤에는 Portal 서버를 재시작하세요.

## 실제 메타데이터 API 연결

`.env`에서 다음 항목을 채웁니다. 실제 `.env` 파일은 Git에 올리지 않습니다.

```dotenv
# preview 대신 api로 바꿉니다.
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

`MONGODB_URI`와 `MONGODB_DATABASE`는 관리자 설정 저장에도 사용합니다. 포털을 화면 미리보기로만 사용할 때는 비워 둘 수 있지만, 관리자 설정을 실제로 저장하려면 두 값을 반드시 설정해야 합니다.

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
- `PTMORE_SCHEDULE_COLLECTION`: 스케줄 등록 정보
- `PTMORE_SCHEDULE_RUN_COLLECTION`: 스케줄 실행 이력

현재 스케줄 화면은 더미 데이터 단계입니다. 따라서 `PTMORE_SCHEDULE_COLLECTION`과 `PTMORE_SCHEDULE_RUN_COLLECTION`은 **향후 MongoDB 저장 구현에서 사용할 대상 이름만 정하는 값**이며, 지금 설정해도 스케줄 등록·수정·삭제가 MongoDB에 기록되지는 않습니다.

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

## 화면 확인

- 관리자 화면: `http://127.0.0.1:8002`
- 일반 사용자 권한 미리보기: `http://127.0.0.1:8002/?preview_role=user`

일반 사용자는 메타데이터 메뉴 또는 등록 버튼을 눌러도 `관리자만 등록 가능합니다` 안내만 보게 됩니다. 현재 이 권한 표시는 디자인 검증용이며, 실제 사번/SSO 연동은 이후 기존 로직을 교체하지 않고 별도 인증 어댑터로 연결할 예정입니다.

## 관리자 권한과 설정 저장

- 메타데이터 등록, 메타데이터 API 상태 확인, 관리자 설정 API는 서버에서도 활성 관리자 사번을 확인합니다. 일반 사용자가 브라우저 주소나 API를 직접 호출해도 `403`으로 차단됩니다.
- 설정 화면의 `GAIA API 호출 권한 사번`과 활성 사용자 기준은 MongoDB의 고정 `portal_settings` 컬렉션에 저장됩니다. 변경 시점·변경 관리자·변경 전후 값은 `portal_audit_log`에 기록됩니다.
- GAIA API Key, Bearer Token, MongoDB URI와 같은 비밀값은 계속 `.env` 또는 Secret 관리 도구에서만 관리합니다. 관리자 설정 API는 이 값들을 반환하지 않습니다.

현재 화면 미리보기는 사번 조회 로직이 확정되기 전이므로 `X-PTMORE-Employee-Id` 헤더를 임시 사용자 식별 어댑터로 사용합니다. 운영 배포 시에는 이 헤더를 브라우저가 임의로 보내게 두면 안 되며, SSO 또는 신뢰된 사내 프록시가 사번을 주입하고 외부에서 보낸 동일 헤더를 제거해야 합니다. 사번 조회 방식이 정해지면 이 어댑터만 교체하면 됩니다.
