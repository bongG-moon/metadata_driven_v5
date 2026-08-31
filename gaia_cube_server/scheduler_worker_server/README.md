# PTMORE 독립 스케줄 Worker

이 폴더는 CUBE callback 서버와 별도로 배포·실행하는 HCP 스케줄 Worker입니다. `production_callback_server/app.py`나 Portal 웹 서버를 import하지 않습니다.

포함 파일은 모두 이 폴더 안에서 동작합니다.

- `app.py`: HCP Uvicorn 진입점, Worker 시작·중지 및 `/health`·`/ready`
- `scheduler_worker.py`: MongoDB due 스케줄 claim·GAIA 실행·CUBE 개인 DM 발송 로직
- `cube_runtime.py`: GAIA external API, CUBE Rich Notification payload 및 오류 처리
- `markdown_rich_notification.py`: GAIA Markdown을 CUBE `body.row`로 변환하는 렌더러

## 설치와 실행

```powershell
cd C:\...\gaia_cube_server\scheduler_worker_server
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 또는 HCP Secret에 실제 GAIA, CUBE, MongoDB 값을 입력한 뒤 장기 실행합니다. 운영 HCP는 이 폴더의 `app.py`를 실행 대상으로 사용합니다.

```powershell
python app.py
```

`app.py`는 고정된 `0.0.0.0:5000` Uvicorn 진입점으로 실행됩니다. HCP의 liveness probe는 `/health`, Worker가 실제로 설정·MongoDB 연결을 완료했는지 보는 readiness probe는 `/ready`로 지정합니다.

- `/health`: HTTP 200. 설정 오류가 있어도 프로세스가 살아 있는지 확인합니다.
- `/ready`: 준비 완료면 HTTP 200, 환경변수·MongoDB 초기화 실패 또는 Worker 중단이면 HTTP 503입니다. URI·토큰·원격 오류 원문은 응답에 표시하지 않습니다.

서버 시작 직후 `/ready`가 503이면 HCP Secret의 GAIA/CUBE/MongoDB 값과 MongoDB 네트워크 권한을 확인한 뒤 재배포하세요. `app.py`가 준비 상태가 아닐 때는 GAIA나 CUBE에 스케줄 요청을 보내지 않습니다.

현재 due 상태인 스케줄만 한 번 실행하고 종료하려면 다음을 사용합니다. 실제 GAIA와 CUBE가 호출되므로 테스트용 스케줄만 사용하세요.

```powershell
python scheduler_worker.py --once
```

## Portal 스케줄 계약

Portal이 아래 항목을 MongoDB에 저장합니다. 사람이 보는 `next_run` 문구가 아니라 `next_run_at` UTC ISO 문자열을 사용합니다.

```json
{
  "_id": "SCH-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "owner_id": "2069026",
  "owner_name": "문봉건",
  "question": "DA 공정 실시간 생산 분석을 진행해줘.",
  "status": "active",
  "repeat": "interval",
  "interval_minutes": 10,
  "start_time": "08:00",
  "end_time": "18:00",
  "timezone": "Asia/Seoul",
  "next_run_at": "2026-08-31T01:10:00+00:00"
}
```

반복 값은 `평일`, `매일`, `매주`, `매월`, `한 번만`, `interval`을 지원합니다. `interval`은 `interval_minutes`, `start_time`, `end_time`을 사용합니다. `owner_id`는 사번 형식일 때만 CUBE 개인 DM 수신자로 사용하며 `owner_name`은 표시용입니다.

## 실행 규칙

1. 여러 Worker가 있어도 MongoDB `find_one_and_update` lease로 같은 예정 건을 동시에 실행하지 않습니다.
2. 매 실행마다 `cube_scheduling_<사번>_<UUID>` 새 GAIA 세션을 만들고 `metadata.platform`에 `CUBE_SCHEDULING`을 넣습니다.
3. 일반 callback의 “처리중” 메시지는 보내지 않습니다.
4. 결과는 등록자 개인 DM으로만 전송하며, 항상 아래 접두어를 사용합니다.

```text
안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀.
실행 질문 : <등록 질문>

<GAIA 답변 또는 안전한 안내>
```

5. 실행 이력은 `PTMORE_SCHEDULE_RUN_COLLECTION`에 `status`, `error_category`, `scheduled_for`, `started_at`, `completed_at`, `delivery_status`로 저장합니다.
6. Portal이 편집·중지·삭제로 lease를 무효화하거나 lease가 만료되면, Worker는 CUBE 발송 직전에 재확인하여 이전 결과를 보내지 않습니다.

`PTMORE_SCHEDULER_LEASE_SECONDS`는 `GAIA_TIMEOUT_SECONDS + CUBE_TIMEOUT_SECONDS + 60초` 이상이어야 합니다. Worker가 강제 종료되면 lease 만료 뒤 재실행될 수 있으므로, 장애 복구는 at-least-once 방식입니다.

MongoDB `createIndex` 권한은 선택입니다. 없으면 Worker는 경고만 남기고 기존 인덱스로 계속 동작합니다.
