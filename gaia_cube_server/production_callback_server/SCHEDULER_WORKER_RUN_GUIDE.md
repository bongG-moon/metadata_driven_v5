# PTMORE 스케줄 실행기 운영 안내

> 실제 운영에서 callback 서버와 Worker가 분리되어 있다면 이 폴더의 `scheduler_worker.py`를 배포하지 마세요. 독립 배포용 패키지인 [../scheduler_worker_server/README.md](../scheduler_worker_server/README.md)를 사용해야 합니다. 이 문서는 기존 co-deploy 구현의 동작 계약을 보관합니다.

`scheduler_worker.py`는 `app.py`와 **별도 프로세스**로 실행하는 장기 실행 작업자입니다.

- `app.py`: CUBE 사용자의 일반 채팅 callback을 받고 GAIA 답변을 보냅니다.
- `scheduler_worker.py`: MongoDB에 저장된 실행 예정 스케줄을 읽어 GAIA 실행 후, 등록자 개인 DM으로 결과를 보냅니다.

두 프로세스는 같은 GAIA/CUBE `.env` 값을 사용하지만 서로 대체하지 않습니다. HCP에서는 app 컨테이너/프로세스와 Worker 컨테이너/프로세스를 각각 하나씩 운영하세요. Worker를 여러 개 띄울 수는 있지만, MongoDB의 원자적 claim으로 동일 예정 건을 동시에 실행하지 않습니다.

## 1. 환경 변수

기존 GAIA/CUBE 값에 아래 값을 추가합니다. 실제 분리 배포의 전체 예시는 [../scheduler_worker_server/.env.example](../scheduler_worker_server/.env.example)에 있습니다.

```dotenv
MONGODB_URI=mongodb://<계정>:<비밀번호>@<호스트>/<옵션>
MONGODB_DATABASE=datagov_test
PTMORE_SCHEDULE_COLLECTION=portal_schedules
PTMORE_SCHEDULE_RUN_COLLECTION=portal_schedule_runs

PTMORE_SCHEDULER_POLL_SECONDS=30
PTMORE_SCHEDULER_LEASE_SECONDS=900
PTMORE_SCHEDULER_BATCH_SIZE=20
PTMORE_SCHEDULER_MONGODB_TIMEOUT_MS=5000
```

`PTMORE_SCHEDULER_LEASE_SECONDS`는 `GAIA_TIMEOUT_SECONDS + CUBE_TIMEOUT_SECONDS + 60초` 이상이어야 하며, Worker는 더 짧은 값으로 시작하지 않습니다. 처리 중 Worker가 강제 종료되면 lease가 만료된 뒤 다른 Worker가 다시 실행할 수 있으므로, 이 실행기는 **동시 중복은 막고 장애 뒤 재시도는 허용하는 at-least-once 방식**입니다.

`MONGODB_URI`, `GAIA_AUTH_KEY`, `CUBE_BOT_TOKEN`은 소스 코드나 Git에 넣지 말고 HCP Secret 또는 배포 환경 변수로 관리합니다.

## 2. Portal이 저장할 스케줄 문서

Portal은 아래처럼 `next_run_at`을 반드시 UTC ISO 문자열로 저장합니다. Worker는 사람이 보는 `next_run` 문구를 사용하지 않습니다.

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

필수 항목은 `_id`, `owner_id`, `question`, 활성 상태, `repeat`, `next_run_at`입니다. Portal API는 `_id` 값을 화면용 `id`로 돌려주며, `owner_name`은 화면 표시용이고 CUBE 수신자로 사용하지 않습니다.

| 반복 값 | 추가 입력 | 동작 |
| --- | --- | --- |
| `매일` / `daily` | `time: "09:30"` | 매일 KST 지정 시각 |
| `평일` / `weekdays` | `time` | 월~금 KST 지정 시각 |
| `매주` / `weekly` | `time`, 선택 `weekday` (`0`=월~`6`=일) | 매주 지정 요일 |
| `매월` / `monthly` | `time`, 선택 `day_of_month` | 지정 일자, 없는 일자는 말일 |
| `한 번만` / `once` | `time` | 한 번 실행 뒤 `inactive` 처리 |
| `interval` 또는 `10분마다` | `interval_minutes`, `start_time`, `end_time` | KST 시작/종료 시간 창 안에서 반복 |

활성 상태는 `active` 또는 `활성`을 사용합니다. `inactive`, `비활성`, `일시중지` 등은 Worker가 실행하지 않습니다. 실행할 수 없는 활성 문서(사번, 질문, 반복 규칙 누락 등)는 무한 반복 발송을 막기 위해 Worker가 `inactive`로 바꾸고 실행 이력에 `invalid_schedule`을 남깁니다.

## 3. 실행

의존성을 한 번 설치합니다.

```powershell
cd C:\...\gaia_cube_server\scheduler_worker_server
python -m pip install -r requirements.txt
```

실제 운영 Worker는 계속 실행합니다.

```powershell
python scheduler_worker.py
```

현재 due 상태인 스케줄만 한 번 실행하고 종료하는 점검은 아래처럼 합니다. 실제 GAIA와 CUBE를 호출하므로 테스트용 등록자/질문만 사용하세요.

```powershell
python scheduler_worker.py --once
```

## 4. 실행 결과와 CUBE 발송 규칙

한 스케줄 실행마다 다음을 수행합니다.

1. `portal_schedules`에서 due + active 문서를 찾고 MongoDB `find_one_and_update`로 lease를 획득합니다.
2. `portal_schedule_runs`에 `running` 이력을 먼저 기록합니다.
3. `cube_scheduling_<사번>_<랜덤 UUID>` 새 GAIA 세션을 만들고 `metadata.platform`에 `CUBE_SCHEDULING`을 넣어 GAIA를 호출합니다.
4. 등록자 사번을 수신자로 하고 **채널 ID 없이 개인 DM**으로 Rich Notification을 한 번 보냅니다.
5. 실행 완료 시 다음 접두어 뒤에 GAIA 답변 또는 안전한 오류 안내를 붙입니다.

```text
안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀.
실행 질문 : <등록 질문>

<GAIA 답변 또는 오류 안내>
```

일반 callback과 달리 `요청하신 내용을 처리중입니다...` 안내 메시지는 보내지 않습니다. 실행 이력에는 `status`, `error_category`, `scheduled_for`, `started_at`, `completed_at`, `delivery_status`가 남으며, 원격 URL·토큰·예외 원문은 저장하지 않습니다.

## 5. MongoDB 접근 권한

Worker 계정은 최소한 다음 컬렉션에 `find`, `insert`, `update` 권한이 있어야 합니다. `createIndex` 권한이 있으면 실행 시 조회용 인덱스를 자동 생성하며, 없으면 경고만 남기고 계속 동작합니다.

- `PTMORE_SCHEDULE_COLLECTION`
- `PTMORE_SCHEDULE_RUN_COLLECTION`

Portal은 스케줄 생성·수정·활성화/비활성화를 담당하고, Worker는 실행 상태와 다음 실행 시각만 갱신합니다. 따라서 Portal의 소유자/관리자 권한 검사는 Worker가 아닌 Portal API에서 유지해야 합니다.

Portal에서 스케줄을 수정하거나 일시중지하면 현재 Worker lease를 무효화합니다. Worker는 GAIA 응답 뒤 CUBE 발송 직전에 lease가 아직 유효하고 활성 상태인지 다시 확인하므로, 느린 GAIA 호출 중 사용자가 중지한 스케줄의 이전 답변은 발송하지 않습니다.
