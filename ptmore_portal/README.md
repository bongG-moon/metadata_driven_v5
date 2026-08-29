# PTMORE PKG Agent Portal

이 포털은 현재 화면 설계와 더미 이력을 유지하면서, **메타데이터 등록만 외부 rev_2 Flow API로 실행**할 수 있게 구성되어 있습니다.

- 대시보드, 사번/권한 미리보기, 사용 이력, 스케줄 화면은 기존 더미 로직을 그대로 사용합니다.
- 메타데이터 등록 요청은 포털 서버가 외부 API로 전달합니다. 브라우저에는 API 키나 MongoDB URI가 내려가지 않습니다.
- CUBE callback 서버와는 별도입니다. 이 포털은 CUBE 메시지를 직접 수신하거나 발송하지 않습니다.

## 동작 흐름

```text
브라우저 등록 화면
  -> 포털 POST /api/metadata-authoring
  -> 포털 서버의 .env 설정
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
python app.py
```

브라우저에서 `http://127.0.0.1:8002`를 엽니다.

초기 `.env`는 `PTMORE_METADATA_API_MODE=preview`이므로 안전한 미리보기 결과만 보여 줍니다. 외부 API나 MongoDB에는 연결하지 않습니다.

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
PTMORE_METADATA_API_USER_ID_HEADER=X-Gaia-User-Id
PTMORE_METADATA_API_USER_ID=<authorized-user-id>

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

### MongoDB 값 전달 기준

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

`GET /api/metadata-authoring/status`는 연결 준비 상태만 반환하며 API 키나 MongoDB URI는 반환하지 않습니다.

## rev_2 Flow에 대한 영향

Flow JSON이나 Custom Component는 변경하지 않았습니다. 기존 Canvas 실행과 기본값은 동일합니다.

포털은 API 호출할 때만 Langflow의 `tweaks`를 이용해 요청 로더의 `duplicate_action`, `dry_run` 및 필요 시 MongoDB 입력을 덮어씁니다. 기본값은 rev_2의 고유한 한국어 노드 표시명을 사용하므로 재-import로 ID가 바뀌어도 수정할 필요가 없습니다. 운영자가 표시명까지 바꾼 경우에만 `PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON`에 새 표시명 또는 node ID를 지정하면 됩니다.

## 화면 확인

- 관리자 화면: `http://127.0.0.1:8002`
- 일반 사용자 권한 미리보기: `http://127.0.0.1:8002/?preview_role=user`

일반 사용자는 메타데이터 메뉴 또는 등록 버튼을 눌러도 `관리자만 등록 가능합니다` 안내만 보게 됩니다. 현재 이 권한 표시는 디자인 검증용이며, 실제 사번/SSO 연동은 이후 기존 로직을 교체하지 않고 별도 인증 어댑터로 연결할 예정입니다.
