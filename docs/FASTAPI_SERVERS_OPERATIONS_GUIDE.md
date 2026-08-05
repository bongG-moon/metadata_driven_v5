# FastAPI Artifact/CUBE Scheduler 서버 운영 가이드

## 1. 서비스 경계

| 프로세스 | 기본 포트 | 읽기 | 쓰기 |
| --- | ---: | --- | --- |
| Artifact Server | 8765 | `datagov.agent_v4_result_store` | HTML Report 저장 경로 |
| CUBE Scheduler Server | 8770 | 외부 `cube_authoring.cube_schedules` | 별도 `cube_scheduler_runtime` DB |
| 12번 Langflow Schedule Saving Flow | Langflow | authoring 후보 조회 | 외부 `cube_authoring.cube_schedules` |

Scheduler는 스케줄 원본에 쓰지 않는다. 원본 MongoDB 계정에는 `find` 권한만 부여하고, cursor/run/outbox/inbound 상태는 별도 runtime 계정과 DB에 기록한다. 예약 시간이 되면 질문을 CUBE 챗봇으로 전달하며, 이 메시지 전달이 Agent 실행 요청이다.

## 2. 설치와 실행

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python -m pip install -e .
python -m artifact_server
```

다른 프로세스 또는 서비스 단위에서 Scheduler를 실행한다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python -m cube_scheduler_server
```

두 앱 모두 Uvicorn worker를 1개로 시작한다. 여러 replica가 필요하면 앱 내부 worker 수를 늘리지 말고, Artifact 저장소와 Scheduler lease/outbox 계약을 유지하는 배포 플랫폼에서 replica 단위로 확장한다.

## 3. 핵심 환경변수

```env
ARTIFACT_LISTEN_HOST=0.0.0.0
ARTIFACT_LISTEN_PORT=8765
ARTIFACT_PUBLIC_BASE_URL=https://artifact.example.internal
ARTIFACT_MAX_CONCURRENT_DOWNLOADS=4
MONGODB_URI=mongodb://result-reader@result-host:27017
MONGODB_DATABASE=datagov
MONGODB_RESULT_COLLECTION=agent_v4_result_store
REPORT_STORAGE_DIR=D:\metadata-driven\reports
REPORT_USE_ACCESS_TOKEN=true

CUBE_SERVER_HOST=0.0.0.0
CUBE_SERVER_PORT=8770
CUBE_SCHEDULE_SOURCE_MONGODB_URI=mongodb://schedule-reader@schedule-host:27017
CUBE_SCHEDULE_SOURCE_DATABASE=cube_authoring
CUBE_SCHEDULE_SOURCE_COLLECTION=cube_schedules
CUBE_RUNTIME_MONGODB_URI=mongodb://scheduler-runtime@runtime-host:27017
CUBE_RUNTIME_DATABASE=cube_scheduler_runtime
CUBE_OUTBOUND_URL=https://cube.example.internal/richnotification
CUBE_BOT_ID=
CUBE_BOT_TOKEN=
CUBE_CALLBACK_ADDRESS=https://scheduler.example.internal/api/cube/callback
```

`ARTIFACT_LISTEN_HOST`는 bind 주소이고 `ARTIFACT_PUBLIC_BASE_URL`은 사용자가 클릭할 링크 기준이다. `0.0.0.0`을 공개 URL로 사용하지 않는다. 두 앱은 기본적으로 저장소 루트의 `.env`를 읽으며 별도 파일은 `ARTIFACT_ENV_FILE`, `CUBE_ENV_FILE`로 지정할 수 있다.

## 4. 스케줄 문서 계약

```json
{
  "contract": "cube.schedule.v1",
  "schedule_id": "schedule:2000000:weekday-wip",
  "version": 1,
  "employee_id": "2000000",
  "channel_id": "500000000",
  "question": "현재 WIP 상위 제품과 병목 공정을 알려줘",
  "schedule": {
    "type": "cron",
    "expression": "0 8 * * 1-5",
    "timezone": "Asia/Seoul"
  },
  "enabled": true,
  "updated_at": "2026-08-05T01:00:00Z"
}
```

interval은 최소 5분이며 cron은 5-field만 허용한다. `channel_id`가 없으면 runtime의 `employee_id → channel_id` 매핑을 사용하고, 둘 다 없으면 source를 수정하지 않은 채 `invalid_definition`으로 진단한다. source 건수가 `CUBE_SCHEDULE_SOURCE_LIMIT`에 닿으면 누락 스케줄 오판을 막기 위해 해당 reconciliation 전체를 중단한다.

12번 Flow는 기본 `dry_run=true`다. 검토 후 Writer 노드에서 MongoDB URI를 authoring DB에 연결하고 `dry_run=false`로 바꿔야 실제 upsert가 수행된다.

## 5. 상태 확인과 API

```text
GET http://127.0.0.1:8765/live
GET http://127.0.0.1:8765/ready
GET http://127.0.0.1:8770/live
GET http://127.0.0.1:8770/ready
```

Artifact `/ready`는 Result MongoDB ping과 Report 저장 경로 쓰기 probe를 확인한다. Scheduler `/ready`는 설정, source MongoDB, runtime MongoDB를 각각 확인한다.

Scheduler의 `/api/v1/schedules`, `/api/v1/schedules/{id}`, `/api/v1/schedules/{id}/runs`는 진단용 읽기 API다. 스케줄 POST/PATCH/DELETE route는 제공하지 않는다. 외부 공개가 필요하면 reverse proxy에서 TLS, SSO 또는 서비스 인증, IP 제한을 적용한다.

## 6. 재시도와 재기동

- `schedule_id + scheduled_for UTC`로 실행을 멱등 식별한다.
- CUBE timeout, 408/425/429/5xx는 지수 backoff 후 재시도한다.
- 비재시도 4xx 또는 최대 시도 초과는 `dead_letter`로 남긴다.
- 전송 도중 종료된 `sending` outbox는 lease 만료 후 다른 worker가 다시 claim한다.
- 운영 프로세스 재시작은 Kubernetes, systemd 또는 Windows Service가 담당한다. 앱이 임의로 기존 포트 프로세스를 종료하지 않는다.

## 7. 배포 전 필수 확인

- source 계정으로 insert/update/delete가 거부되는지 확인
- runtime 계정으로 index 생성과 cursor/run/outbox 쓰기가 가능한지 확인
- 실제 CUBE 테스트 사용자에게 예약 질문이 한 번만 전달되는지 확인
- CUBE 429/5xx와 timeout에서 retry 후 정상 복구되는지 확인
- Artifact 공개 링크가 내부 bind 주소가 아니라 HTTPS 도메인을 반환하는지 확인
- Report volume 백업, 용량 제한, TTL 정리와 reverse proxy request/response timeout 설정
