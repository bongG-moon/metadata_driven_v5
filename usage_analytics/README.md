# MongoDB 사용 현황 커스텀 집계

웹 서버/Flask/Phoenix 없이 실행합니다. 필요한 패키지는 `pip install "pymongo>=4.6,<5"`입니다. DB 조회만 수행하고 인덱스 생성이나 데이터 변경은 하지 않습니다.

## 파일 상단에서 조건 입력 (권장)

`aggregate_usage.py` 상단의 아래 값을 수정하세요. 명령줄 인자는 더 이상 필수가 아닙니다.

```python
START_DATE = "2026-09-01"
END_DATE = "2026-09-28"
MIN_USAGE_DAYS = 3
MIN_CHAT_COUNT = 10
ACTIVE_START_DATE = None  # 전체 기간과 동일. 별도 기간이면 시작/종료 모두 입력
ACTIVE_END_DATE = None
PROJECTS = None          # 전체 또는 ["프로젝트A", "프로젝트B"]
```

MongoDB 환경변수를 설정했다면 `python aggregate_usage.py`로 실행합니다. `.env`를 사용하면 `python -m dotenv -f .env run -- python aggregate_usage.py`로 실행합니다. 기존 명령줄 옵션도 지원하며 입력 시 파일 상단 값보다 우선합니다.

출력 JSON의 `raw_records`에서 프로젝트·일자·시간·플랫폼·사번·이름·질문·trace ID를 확인할 수 있습니다. 이는 최신 갱신 기준으로 선별된 **MongoDB 보관 원본 이력**이며 Phoenix 전체 span 또는 제외된 과거 갱신 문서는 아닙니다. 전체/활성 기간이 다르면 두 기간의 합집합을 표시하고 `in_usage_period`, `in_active_period`로 각 집계 포함 여부를 표시합니다.

`usage`와 `active_period`에는 각각 요약 수치, 일별 `daily`, 전체 사용자별 `users`, 활성 사용자별 `active_users`가 있습니다. 원본 질문과 사번이 포함되므로 결과를 공유할 때 주의하세요.

## Python / Notebook에서 사용

이 폴더의 `aggregate_usage.py`를 작업 폴더에 복사하고 아래처럼 호출하세요. URI는 비공개 환경변수 또는 별도 설정에서 전달하세요.

```python
import os
from datetime import date
from aggregate_usage import query_usage

result = query_usage(
    uri=os.environ["MONGODB_URI"],
    database="datagov",
    collection="portal_usage_history",
    start=date(2026, 9, 1),
    end=date(2026, 9, 28),
    active_start=date(2026, 9, 8),  # 생략하면 전체 집계 기간과 동일
    active_end=date(2026, 9, 28),
    min_days=3,                    # 서로 다른 사용 일자 수 이상
    min_chats=10,                  # 채팅 건수 이상 (두 조건 모두 충족)
    projects=None,                 # 전체. 특정 프로젝트: ["프로젝트A", "프로젝트B"]
)

print(result["usage"]["total_users"])
print(result["usage"]["total_chats"])
print(result["usage"]["daily_average_users"])
print(result["usage"]["daily_average_chats"])
print(result["active_period"]["active_user_count"])
print(result["active_period"]["active_users"])
```

## 명령줄

`MONGODB_URI` 환경변수를 비공개로 설정한 후 실행합니다. `.env`나 Portal 설정 파일을 자동으로 읽지는 않습니다.

### 함께 제공된 환경변수 예시 파일 사용

압축을 푼 폴더에서 `.env.example`을 `.env`로 복사하고 실제 MongoDB URI를 입력하세요. 아래 명령은 `.env`를 읽어서 집계 코드에 환경변수를 전달합니다. 코드를 수정할 필요는 없습니다.

```powershell
python -m pip install "pymongo>=4.6,<5" "python-dotenv[cli]>=1,<2"
Copy-Item .env.example .env
# .env의 YOUR_USER, YOUR_PASSWORD, YOUR_CLUSTER를 실제 값으로 수정한 후:
python -m dotenv -f .env run -- python aggregate_usage.py --start 2026-09-01 --end 2026-09-28 --active-start 2026-09-08 --active-end 2026-09-28 --min-days 3 --min-chats 10
```

이미 `.env`가 있다면 복사 명령은 건너뛰세요. 실제 접속 정보가 들어간 파일은 공유하지 마세요. Python/Notebook에서 사용하려면 함수 호출 전에 `from dotenv import load_dotenv; load_dotenv(".env", override=False)`로 읽을 수 있습니다.

### 환경변수가 이미 설정된 경우

```powershell
python aggregate_usage.py --start 2026-09-01 --end 2026-09-28 --active-start 2026-09-08 --active-end 2026-09-28 --min-days 3 --min-chats 10 --database datagov
```

결과는 Python 파일 옆 `reports` 폴더에 HTML과 JSON으로 저장되며 터미널에 절대 경로가 출력됩니다. HTML 파일을 더블클릭해서 브라우저로 열면 됩니다. 외부 인터넷 연결 없이 요약 지표, 일별 집계, 사용자별 집계, 활성 명단, 원본 이력을 볼 수 있습니다. 각 표는 검색과 25건 단위 페이지 이동을 지원하며 원본 행을 잘라내지 않습니다.

저장 위치는 파일 상단 `OUTPUT_DIRECTORY` 또는 `--output-dir "C:/원하는/폴더"`로 변경합니다. 실행마다 새 파일을 생성합니다. Notebook에서 `query_usage()`만 호출하면 dict만 반환하므로, HTML도 만들려면 `from aggregate_usage import save_report` 후 `save_report(result, "reports")`를 호출하세요.

프로젝트별 제한은 `--project "프로젝트A" --project "프로젝트B"`처럼 지정합니다. HTML/JSON 모두 사번과 질문이 포함되므로 안전하게 보관하고 Git에 올리지 마세요.

## 집계 기준

- 시작일과 종료일 모두 포함, 저장된 KST `usage_date` 기준입니다.
- 전체 사용자: 기간 내 고유 사번 수. 여러 프로젝트의 동일 사번은 1명입니다.
- 전체 채팅: 보관된 유효 이력 행 수. 스케줄링 질문도 포함합니다.
- 일 평균 사용자: 매일의 고유 사용자 수 합계 / 기간의 전체 날짜 수.
- 일 평균 채팅: 전체 채팅 / 기간의 전체 날짜 수. 0건 날짜도 분모에 포함합니다.
- 활성 사용자: 지정 기간 동안 사용 일수와 채팅 건수 기준을 **모두** 충족한 사용자입니다. 기간은 전체 지표와 별도로 지정할 수도 있습니다.
- 사용자별 `daily_average`는 전체 기간 기준, `usage_day_average`는 실제 사용한 날짜 수 기준입니다.
- `usage`는 전체 지표 기간, `active_period`는 활성 사용자 기간 집계이며 각각 `users`, `active_users`, `daily` 목록도 포함합니다.
- 수집 완료 마커의 `refresh_started_at`과 일치하는 갱신 데이터만 포함합니다. 마커 없는 과거 데이터는 기존 Portal처럼 읽습니다. 마커와 잠금 문서는 채팅으로 세지 않습니다.
- 사번 누락은 기존 Portal과 동일하게 `미확인` 한 명으로 계산합니다. `unknown_user_chat_count`로 확인하세요.
- DB에 미수집된 날짜는 0건으로 반영됩니다. 이 코드는 누락된 Phoenix 데이터를 복구하거나 데이터를 새로 적재하지 않습니다.
