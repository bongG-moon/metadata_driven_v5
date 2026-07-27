# 실시간 생산 분석 Report 운영 환경 구현 가이드

## 1. 문서 목적

이 문서는 `11. v5_realtime_production_report` 예시 Flow를 실제 생산 환경에 적용하기 위한 구현 기준을 정의한다.

현재 예시는 약 500행의 판정 더미 데이터를 내부에서 생성하지만, 운영 환경에서는 기존 생산 판정 Python Job이 생성한 완료 Snapshot을 조회해야 한다. 운영 전환 시 Report의 집계·메시지·HTML 생성 규칙을 다시 구현하지 않고, 더미 데이터 생성 노드만 실제 Snapshot Loader로 교체하는 것을 기본 원칙으로 한다.

이 문서의 범위는 다음과 같다.

- 실제 판정 데이터와 Report Flow의 역할 분리
- Snapshot 저장 및 완료 처리 방식
- `production.judgement.dataset.v1` 데이터 계약
- 실제 Snapshot Loader의 입력·출력·검증 규칙
- 고정 공정형 및 채팅 조건형 Flow 구성
- 채팅 응답과 HTML Report의 출력 범위
- Report API 서버의 운영 배포
- 보안, 대용량 다운로드, 모니터링 및 장애 정책
- 단계별 전환 계획과 운영 승인 기준

현재 기준 런타임은 다음과 같다.

| 구분 | 기준 |
| --- | --- |
| Langflow | `1.9.2` |
| langflow-base | `0.9.2` |
| LFX | `0.4.2` |
| Python | `3.12` 권장 |
| Realtime Report Flow | `11. v5_realtime_production_report` |
| 통합 Report 서버 | `tools/data_ref_download_server.py` |

---

## 2. 운영 전환의 핵심 결정

운영 구현은 아래 구조를 권장한다.

```mermaid
flowchart LR
    A["기존 생산 판정 Python Job"] --> B["판정 결과 검증"]
    B --> C[("완료된 생산 Snapshot 저장소")]
    D["사용자 질문 또는 정기 실행"] --> E["조회조건 결정"]
    E --> F["실시간 생산 Snapshot Loader"]
    C --> F
    F --> G["실시간 생산 분석 Report 생성기"]
    G --> H["채팅 요약"]
    G --> I["통합 Report API :8765"]
    I --> J["HTML 보기·다운로드"]
```

### 2.1 권장 원칙

1. 기존 판정 Python Job이 모든 업무 판정을 소유한다.
2. Report Flow는 `달성율*판정`, `CAPA이상판단` 등의 판정식을 다시 계산하지 않는다.
3. Report Flow는 완료된 판정 Snapshot을 집계하고 화면용 상위 분류만 생성한다.
4. 판정 데이터가 저장 중인 상태에서는 조회하지 않는다.
5. Snapshot Loader는 `status=complete`인 최신 Snapshot만 읽는다.
6. 채팅에는 요약과 링크만 전달하고 원본 행과 HTML 본문은 전달하지 않는다.
7. HTML 상세 표와 전체 데이터 다운로드는 분리 가능한 구조로 운영한다.

### 2.2 권장하지 않는 방식

- Langflow Custom Component 안에 기존 판정 Python 코드 전체를 중복 구현
- 저장 중인 운영 테이블을 완료 여부 확인 없이 직접 조회
- LLM이 생산 판정값이나 최종 집계 숫자를 임의로 생성
- 사용자 질문에서 추출한 공정명을 허용목록 검증 없이 쿼리에 사용
- 수천 행의 원본 데이터를 Chat Output이나 Workflow observation에 포함
- 운영 Report 서버를 인증 없이 외부 인터넷에 직접 노출

---

## 3. 현재 구현과 운영 구현의 경계

현재 Flow는 다음과 같이 연결되어 있다.

```text
00 실시간 생산 판정 더미 데이터.dataset
  -> 01 실시간 생산 분석 Report 생성기.dataset

Chat Input
  -> 01 실시간 생산 분석 Report 생성기.question

01 Report 생성기.message
  -> GaiA Output Adapter
  -> Chat Output

01 Report 생성기.api_response
  -> 02 API 종료 어댑터
```

운영 전환 후에는 첫 번째 연결만 아래와 같이 변경한다.

```text
00 실시간 생산 판정 Snapshot Loader.dataset
  -> 01 실시간 생산 분석 Report 생성기.dataset
```

`01 실시간 생산 분석 Report 생성기`, Chat Output, API 종료 어댑터 및 Report API 계약은 그대로 유지한다.

### 3.1 컴포넌트별 책임

| 컴포넌트 | 책임 |
| --- | --- |
| 생산 판정 Python Job | 원천 데이터 조회, 업무 계산, 전체 판정 컬럼 생성 |
| Snapshot 저장소 | 불변 판정 행과 Snapshot 완료 상태 보관 |
| 조회조건 결정기 | 기준일·공정·Snapshot 조건을 결정론적으로 확정 |
| Snapshot Loader | 최신 완료 Snapshot 조회, 타입 정규화, 계약 검증 |
| Report 생성기 | 고정 Rule 집계, 채팅 요약, HTML 생성 |
| Report API 서버 | HTML 등록, 보기·다운로드 URL 발급, TTL 정리 |
| Router/Orchestrator | 사용자 요청을 전용 Report Flow로 전달 |

---

## 4. 실제 데이터 공급 방식

### 4.1 방식 비교

| 방식 | 설명 | 권장도 | 적용 조건 |
| --- | --- | --- | --- |
| 완료 Snapshot DB 조회 | 판정 Job이 DB에 저장한 완료 Snapshot을 Loader가 조회 | 권장 | DB 접근이 가능하고 배치 완료 상태를 관리할 수 있음 |
| 판정 시스템 REST API | 판정 시스템이 완료 Snapshot API를 제공 | 조건부 권장 | 인증, Timeout, 응답 크기와 장애 정책이 정의되어 있음 |
| 공유 파일 조회 | 완료 CSV/Parquet 파일을 Loader가 읽음 | Pilot 한정 | 파일 완료 마커와 동시 접근 제어가 있음 |
| Flow 내부 판정 재실행 | Langflow가 원천 데이터부터 판정을 다시 계산 | 비권장 | 판정 로직 중복, 실행시간 증가, 버전 불일치 위험 |

운영 기본안은 `완료 Snapshot DB 조회`로 한다.

### 4.2 Snapshot 저장 모델

MongoDB를 사용하는 경우 메타데이터와 행 데이터를 분리한다.

```text
production_judgement_snapshots
production_judgement_rows
```

#### Snapshot 메타데이터 예시

```json
{
  "_id": "production:20260727:143000",
  "snapshot_id": "production:20260727:143000",
  "snapshot_at": "2026-07-27T14:30:00+09:00",
  "work_date": "2026-07-27",
  "status": "complete",
  "row_count": 500,
  "judgement_rules_version": "production-judgement.v12",
  "source_system": "production-analysis-job",
  "created_at": "2026-07-27T14:30:05+09:00",
  "completed_at": "2026-07-27T14:30:18+09:00"
}
```

#### Snapshot 행 예시

```json
{
  "snapshot_id": "production:20260727:143000",
  "case_key": "2026-07-27|WB010|MASS|8G|1A|X4|FBGA|STD|A-663|MCP-1000-B",
  "WORK_DATE": "2026-07-27",
  "OPER": "WB010",
  "OPER_NAME": "W/B1",
  "MODE": "MASS",
  "DENSITY": "8G",
  "TECH": "1A",
  "ORG": "X4",
  "PKG1": "FBGA",
  "PKG2": "STD",
  "LEAD": "A-663",
  "MCP_NO": "MCP-1000-B",
  "달성율*판정": "정상",
  "적정재공*판정": "정상",
  "CAPA판정": "CAPA과다",
  "CAPA이상판단": "정상",
  "장비교체판단": "정상",
  "가동율판정": "정상"
}
```

45개 전체 컬럼은 동일 행에 저장한다. 예시는 식별 및 판정 컬럼만 축약하여 표시했다.

### 4.3 원자적 완료 처리

판정 Job은 다음 순서로 Snapshot을 발행한다.

1. Snapshot 메타데이터를 `status=building`으로 생성한다.
2. 동일 `snapshot_id`를 가진 판정 행을 저장한다.
3. 필수 컬럼, 판정값, 중복 Case, 실제 행 수를 검증한다.
4. 검증에 성공하면 `row_count`, `completed_at`을 기록하고 `status=complete`로 변경한다.
5. 실패하면 `status=failed`와 오류 요약을 기록한다.
6. Loader는 `building`과 `failed` Snapshot을 조회하지 않는다.

이 구조를 사용하면 사용자가 Report를 요청한 시점에 절반만 저장된 데이터를 분석하는 문제를 방지할 수 있다.

### 4.4 권장 인덱스

MongoDB 예시는 다음과 같다.

```text
production_judgement_snapshots:
  { status: 1, snapshot_at: -1 }
  { work_date: 1, status: 1, snapshot_at: -1 }

production_judgement_rows:
  { snapshot_id: 1 }
  { snapshot_id: 1, OPER_NAME: 1 }
  { snapshot_id: 1, case_key: 1 } unique
```

`case_key`는 아래 컬럼을 정규화한 뒤 결합해 생성한다.

```text
WORK_DATE
+ OPER
+ MODE
+ DENSITY
+ TECH
+ ORG
+ PKG1
+ PKG2
+ LEAD
+ MCP_NO
```

---

## 5. Report 입력 데이터 계약

Snapshot Loader는 `production.judgement.dataset.v1` 형식의 Langflow `Data`를 반환한다.

```json
{
  "contract_version": "production.judgement.dataset.v1",
  "snapshot_id": "production:20260727:143000",
  "snapshot_at": "2026-07-27T14:30:00+09:00",
  "source_type": "live",
  "columns": [],
  "row_count": 500,
  "rows": []
}
```

운영 확장 필드로 다음 값을 함께 전달하는 것을 권장한다.

```json
{
  "judgement_rules_version": "production-judgement.v12",
  "source_system": "production-analysis-job",
  "query_scope": {
    "work_date": "2026-07-27",
    "oper_names": ["W/B1", "W/B2", "W/B3", "W/B4"]
  }
}
```

Report 생성기가 아직 사용하지 않는 확장 필드는 무시할 수 있다. 운영 추적을 위해 향후 Report 상단과 API 응답에 `judgement_rules_version`을 노출하는 개선을 권장한다.

### 5.1 전체 컬럼

운영 Snapshot에는 다음 컬럼을 모두 포함한다.

```text
WORK_DATE, MODE, DENSITY, TECH, ORG, PKG1, PKG2, LEAD, MCP_NO,
OPER, OPER_NAME, OPER_SEQ, NETDIE_300_CNT, PRODUCTION, WIP,
INPUT_PLAN, OUT_PLAN, 생산실적달성율, 달성율*판정,
적정재공수량, 적정재공율, 적정재공*판정,
EQP_COUNT, DOWN_CNT, OVER_2H_DOWN, 기준UPH, 보유UPH,
보유CAPA(24H), 보유CAPA(잔여), 잔여목표수량, CAPA확보율,
장비BAL, CAPA판정, CAPA이상판단,
이전공정재공, 현재작업재공, 장비교체판단재공, 재공보유율,
장비교체판단, 장비필요대수,
평균가동율, 평균NOWIP, 가동율목표, 가동율달성률, 가동율판정
```

### 5.2 현재 생성기가 중단 기준으로 사용하는 핵심 컬럼

```text
WORK_DATE, MODE, DENSITY, TECH, ORG, PKG1, PKG2, LEAD, MCP_NO,
OPER, OPER_NAME,
달성율*판정, 적정재공*판정, CAPA판정, CAPA이상판단,
장비교체판단, 가동율판정
```

현재 생성기는 나머지 컬럼이 없으면 경고 후 빈 값으로 표시한다. 운영 환경에서는 전체 45개 컬럼 누락을 Loader 오류로 처리하는 것을 권장한다.

### 5.3 허용 판정값

| 컬럼 | 허용값 |
| --- | --- |
| `달성율*판정` | `정상`, `생산부족`, `Abnormal`, `정상(초과생산)` |
| `적정재공*판정` | `정상`, `재공과다`, `Abnormal` |
| `CAPA판정` | `CAPA과다`, `CAPA부족`, `잉여장비` |
| `CAPA이상판단` | `정상`, `Abnormal`, `CAPA부족`, `생산부진1`, `생산부진2` |
| `장비교체판단` | `정상`, `장비필요`, `교체필요`, `교체불필요` |
| `가동율판정` | `정상`, `Abnormal` |

공백, 대소문자, 특수문자 차이로 새로운 값이 생기지 않도록 판정 Job에서 값을 표준화한다.

---

## 6. 실제 Snapshot Loader 설계

신규 standalone Custom Component의 권장 이름은 다음과 같다.

```text
00 실시간 생산 판정 Snapshot Loader
class name: LiveProductionJudgementSnapshotLoader
```

### 6.1 노드 입력

운영 설정은 환경변수에만 숨기지 않고 Langflow 노드 입력에서도 확인하고 변경할 수 있어야 한다.

| 입력 | 필수 | 설명 |
| --- | --- | --- |
| `mongo_uri` 또는 `source_api_url` | 예 | 실제 판정 Snapshot 저장소 |
| `mongo_database` | DB 사용 시 | Database |
| `snapshot_collection` | DB 사용 시 | Snapshot 메타데이터 컬렉션 |
| `rows_collection` | DB 사용 시 | 판정 행 컬렉션 |
| `work_date` | 아니오 | 빈 값 또는 `latest`이면 최신 기준일 |
| `oper_names` | 아니오 | 허용된 공정명 목록 |
| `snapshot_id` | 아니오 | 특정 Snapshot 재현 시 사용 |
| `max_snapshot_age_minutes` | 예 | 실시간으로 인정할 최대 경과시간 |
| `query_timeout_seconds` | 예 | DB/API Timeout |
| `max_rows` | 예 | 비정상 대량 조회 방지 상한 |
| `strict_validation` | 예 | 운영 기본값 `true` |

### 6.2 처리 순서

```mermaid
flowchart TD
    A["조회조건 입력"] --> B{"snapshot_id 지정?"}
    B -- "예" --> C["해당 Snapshot 조회"]
    B -- "아니오" --> D["최신 complete Snapshot 조회"]
    C --> E["Snapshot freshness 확인"]
    D --> E
    E --> F["대상 공정 행 조회"]
    F --> G["행 수·컬럼·타입·판정값 검증"]
    G --> H["production.judgement.dataset.v1 반환"]
```

### 6.3 조회 규칙

1. 특정 `snapshot_id`가 있으면 그 Snapshot만 조회한다.
2. 없으면 `status=complete` 중 기준일과 공정 조건을 만족하는 최신 Snapshot을 조회한다.
3. Snapshot 시각이 `max_snapshot_age_minutes`보다 오래되면 중단한다.
4. 메타데이터 `row_count`와 전체 Snapshot 행 수가 일치하는지 확인한다.
5. 공정 필터 적용 후 0행이면 빈 정상 결과가 아니라 조회 오류로 처리한다.
6. `max_rows`를 초과하면 전체를 임의 절단하지 않고 오류 또는 별도 대용량 모드로 전환한다.

### 6.4 운영 검증 정책

| 검증 항목 | 현재 Report 생성기 | 운영 Loader 권장 |
| --- | --- | --- |
| 데이터 0행 | 오류 | 오류 |
| 핵심 컬럼 누락 | 오류 | 오류 |
| 선택 컬럼 누락 | 경고 | 전체 45개 기준 오류 |
| 미등록 판정값 | 경고 | 오류 |
| 여러 WORK_DATE 혼재 | 경고 | 오류 |
| Case key 중복 | 경고 | 오류 |
| Snapshot 시간 초과 | 검사 없음 | 오류 |
| 메타데이터 행 수 불일치 | 검사 없음 | 오류 |
| 숫자 컬럼 타입 오류 | 표시 시 문자열 처리 | 오류 또는 명시적 null 처리 |

### 6.5 오류 응답 예시

```json
{
  "contract_version": "production.judgement.dataset.v1",
  "status": "error",
  "success": false,
  "snapshot_id": "",
  "rows": [],
  "warnings": [],
  "errors": [
    {
      "type": "stale_snapshot",
      "message": "최신 완료 Snapshot이 허용시간 10분을 초과했습니다."
    }
  ]
}
```

오류 상태의 데이터를 Report 생성기에 연결하지 않거나, Report 생성기가 명시적으로 오류를 반환하도록 연결한다.

---

## 7. 조회조건 결정 방식

현재 Report 생성기의 `question`은 사용자 메시지와 Report 메타데이터에 사용된다. 질문에서 공정명이나 기준일을 추출해 데이터 조회조건을 변경하지는 않는다.

실제 환경에서는 아래 두 방식 중 하나를 선택한다.

### 7.1 고정 공정형

정형화된 업무에는 고정 공정형을 우선 권장한다.

```text
work_date = latest
oper_names = W/B1, W/B2, W/B3, W/B4
snapshot_mode = latest_complete
max_snapshot_age_minutes = 10
```

장점은 다음과 같다.

- LLM 해석 오류가 없다.
- 동일 질문은 동일 범위의 데이터를 사용한다.
- 운영 검증과 숫자 대조가 단순하다.
- 정기 Report와 채팅 실행이 같은 기준을 사용한다.

### 7.2 채팅 조건형

사용자가 대상 공정이나 기준일을 선택해야 한다면 Loader 앞에 결정론적 요청 해석 노드를 추가한다.

```text
Chat Input
  -> Realtime Production Report Request Resolver
  -> Snapshot Loader
  -> Report 생성기
```

Resolver 출력 예시는 다음과 같다.

```json
{
  "work_date": "latest",
  "oper_names": ["W/B2"],
  "snapshot_max_age_minutes": 10,
  "validation": {
    "allowed_oper_names": true
  }
}
```

LLM을 사용해 후보를 추출할 수는 있지만 다음 검증을 반드시 수행한다.

- 실제 공정 Catalog에 존재하는지 확인
- 허용된 공정 Alias인지 확인
- 최대 공정 선택 개수 제한
- 기준일 형식 검증
- 조건이 모호하면 임의 확정하지 않고 사용자에게 재질문

---

## 8. 집계 Grain과 업무 기준

운영 적용 전에 업무 담당자와 집계 단위를 확정해야 한다.

### 8.1 현재 구현 기준

제품 Key:

```text
MODE + DENSITY + TECH + ORG + PKG1 + PKG2 + LEAD + MCP_NO
```

Case Key:

```text
WORK_DATE + OPER + 제품 Key
```

현재 주요 KPI는 Case 행 기준으로 집계한다. `product_count`는 `OPER`를 제외한 제품 Key의 고유 개수다.

### 8.2 생산부족 원인

생산부족 행에서 다음 조건을 복합원인 Flag로 사용한다.

| 원인 | 판정 조건 |
| --- | --- |
| 적정재공부족 | `적정재공*판정 == Abnormal` |
| CAPA부족 | `CAPA판정 == CAPA부족` |
| 가동율저조 | `가동율판정 == Abnormal` |

PIE CHART는 상호배타적인 주원인을 표시하기 위해 다음 우선순위를 적용한다.

```text
적정재공부족 → CAPA부족 → 가동율저조
```

상세 Radio 필터는 복합원인을 중복 조회한다. 따라서 PIE CHART의 원인 건수와 상세 필터의 원인별 건수 합계는 다를 수 있다.

### 8.3 운영 확정이 필요한 항목

- 생산실적 분모가 Case인지 고유 제품인지
- CAPA 세부분석의 “제품 수”가 공정별 Case인지 고유 제품인지
- 같은 제품이 여러 공정에 존재할 때 각각 집계할지
- 생산부족 주원인 우선순위가 현재 순서가 맞는지
- `적정재공*판정=Abnormal`을 모두 “적정재공부족”으로 볼지
- 정상과 교체불필요를 요약에서 합산할지
- Down 장비 포함 CAPA의 기준 시각

이 기준은 코드 수정 전에 업무 규칙 문서와 검증용 Golden Dataset에 함께 고정한다.

---

## 9. 채팅 응답과 HTML Report의 역할

### 9.1 채팅 출력 범위

채팅은 사용자가 즉시 판단할 수 있는 핵심 항목만 제공한다.

- 데이터 기준일과 Snapshot 시각
- 대상 공정
- 전체 Case와 고유 제품 수
- 정상/초과, Abnormal, 생산부족 건수와 비율
- 생산부족 주원인 건수
- CAPA 이상 건수
- 장비필요 및 교체필요 건수
- 가장 우선적인 Action
- HTML 보기 및 다운로드 링크
- 링크 만료시각

채팅에 원본 rows, 전체 45개 컬럼 표, HTML 본문을 포함하지 않는다.

### 9.2 HTML Report 출력 범위

HTML은 다음 여섯 개 PIE CHART를 제공한다.

1. 생산실적 분석
2. 생산부족 원인
3. 생산부족 CASE 세부 원인
4. CAPA실적
5. CAPA실적 이상
6. 장비Assign 조정

네 개 분석 영역에는 각각 다음 항목을 제공한다.

- 결과요약
- 이후 Action 방안
- Radio 필터
- 제품·공정 검색
- 핵심 컬럼/전체 컬럼 전환
- 상세 데이터 표
- Excel 호환 CSV 다운로드

### 9.3 API 응답 범위

`realtime.production.report.v1` API 응답은 다음 상위 구조만 외부로 전달한다.

```text
report_scope
rules_version
kpis
artifacts
warnings
errors
```

원본 행과 HTML 본문은 Workflow observation에 넣지 않는다.

---

## 10. Report API 서버 운영 배포

현재 `tools/data_ref_download_server.py`는 다음 기능을 같은 8765 포트에서 제공한다.

```text
Data Analysis CSV 다운로드
POST   /reports
GET    /reports/view/<report_id>
GET    /reports/download/<report_id>
DELETE /reports/<report_id>
GET    /health
```

### 10.1 운영 환경변수 예시

```env
MONGODB_URI=mongodb://user:password@mongodb.internal:27017
MONGODB_DATABASE=datagov
MONGODB_RESULT_COLLECTION=agent_v4_result_store

DATA_REF_DOWNLOAD_HOST=0.0.0.0
DATA_REF_DOWNLOAD_PORT=8765

# 사용자 브라우저가 접근할 내부 HTTPS 주소
DATA_REF_DOWNLOAD_BASE_URL=https://production-report.company.internal

REPORT_STORAGE_DIR=D:\production-report\storage
REPORT_DEFAULT_TTL_HOURS=4
REPORT_MAX_TTL_HOURS=168
REPORT_MAX_HTML_BYTES=10485760
REPORT_MAX_STORAGE_BYTES=536870912
REPORT_USE_ACCESS_TOKEN=true
```

Linux 또는 Kubernetes에서는 `REPORT_STORAGE_DIR`을 Persistent Volume 경로로 설정한다.

### 10.2 내부 호출 주소와 사용자 주소 분리

Langflow의 `HTML Report API 주소`에는 Langflow 서버가 접근할 수 있는 주소를 입력한다.

```text
http://production-report-service:8765
```

사용자에게 반환할 주소는 Report 서버의 `DATA_REF_DOWNLOAD_BASE_URL`로 설정한다.

```text
https://production-report.company.internal
```

Kubernetes에서 `127.0.0.1`, `localhost`, `0.0.0.0`은 사용자 브라우저용 주소로 사용하지 않는다.

### 10.3 운영 토폴로지

```mermaid
flowchart LR
    A["Langflow"] -->|"POST /reports"| B["Report Service :8765"]
    B --> C[("Persistent Report Storage")]
    D["사내 사용자"] --> E["HTTPS + 사내 인증 Proxy"]
    E --> B
```

현재 Report 저장 방식은 파일 기반이며 Lock은 프로세스 내부 기준이다. 초기 운영은 아래 구성을 권장한다.

```text
Report Service 1 replica
+ Persistent Volume
+ 내부 HTTPS Ingress/Reverse Proxy
+ 사내 인증
```

다중 Replica가 필요하면 다음 개선이 선행되어야 한다.

- HTML 저장소를 공용 Object Storage 또는 공용 파일 저장소로 교체
- TTL 정리 작업을 단일 Worker로 분리
- 다중 인스턴스 간 동시성 제어
- Report metadata 저장소 공용화

### 10.4 실행 및 상태 확인

```powershell
python tools\data_ref_download_server.py --host 0.0.0.0 --port 8765
```

상태 확인:

```text
GET https://production-report.company.internal/health
```

정상 상태에서는 다음 기능이 모두 `true`여야 한다.

```json
{
  "features": {
    "data_ref_csv": true,
    "html_reports": true
  }
}
```

---

## 11. 보안 기준

### 11.1 필수 보호

- 외부 인터넷에 직접 노출하지 않는다.
- HTTPS를 사용한다.
- 사내 인증 Proxy 또는 SSO를 적용한다.
- `REPORT_USE_ACCESS_TOKEN=true`를 적용한다.
- Reverse Proxy 접근 로그에서도 query token을 마스킹한다.
- MongoDB URI와 계정정보를 로그에 출력하지 않는다.
- HTML 응답에 `Cache-Control: no-store`를 유지한다.
- Content Security Policy와 `nosniff` 헤더를 유지한다.
- Report 저장 경로는 서비스 계정만 쓰기 가능하도록 제한한다.

URL token은 임시 링크 보호 수단이며 사용자 인증을 대체하지 않는다. 운영 환경에서는 인증 Proxy와 함께 사용한다.

### 11.2 데이터 최소화

- Report metadata에 원본 행을 중복 저장하지 않는다.
- Chat Output에 원본 데이터를 넣지 않는다.
- Snapshot Loader 로그에는 `snapshot_id`, 행 수, 처리시간만 기록한다.
- 제품 상세 정보가 보안 대상이면 HTML 표의 컬럼 노출 범위를 역할별로 분리한다.
- 운영 TTL이 끝난 Report는 서버 저장소에서 삭제한다.

---

## 12. 대용량 데이터와 Excel 다운로드

현재 Report 생성기의 제한은 다음과 같다.

| 항목 | 현재 기준 |
| --- | --- |
| HTML 기본 표시 행 | 1,000행 |
| HTML 최대 표시 행 | 5,000행 |
| 생성 HTML 최대 크기 | 8,000,000 bytes |
| Report 서버 HTML 기본 상한 | 10 MiB |
| Report API Timeout | 30초 |

집계는 전체 입력 행을 기준으로 하지만, 현재 HTML 내부 CSV 다운로드는 HTML에 포함된 행만 내려받는다.

### 12.1 5,000행 이하

- 현재 interactive HTML을 그대로 사용한다.
- `max_html_rows`를 예상 최대 행 수에 맞게 설정한다.
- HTML 크기가 8MB를 초과하지 않는지 성능 테스트한다.

### 12.2 5,000행 초과

운영에서는 HTML과 전체 다운로드를 분리한다.

```text
HTML:
  KPI + Chart + 최대 1,000행 Preview

전체 데이터:
  Snapshot 저장소 또는 Result Store 참조
  -> 서버 측 CSV/XLSX 생성
  -> TTL 기반 다운로드 URL
```

권장 추가 API 예시는 다음과 같다.

```text
POST /reports/<report_id>/exports
GET  /reports/<report_id>/exports/<export_id>
```

요청에는 임의 쿼리 문자열을 전달하지 않고, 서버가 검증한 필터 Key만 전달한다.

```json
{
  "section": "production",
  "filter": "shortage",
  "format": "xlsx"
}
```

실제 `.xlsx`가 필요한 경우 현재 “Excel 호환 UTF-8 BOM CSV”와 별도로 서버 측 XLSX 생성 기능을 구현한다.

---

## 13. 장애 및 실패 정책

| 상황 | 처리 |
| --- | --- |
| 완료 Snapshot 없음 | Flow 중단, 사용자에게 최신 판정 데이터가 없음을 안내 |
| Snapshot 시간 초과 | Flow 중단, 마지막 Snapshot 시각과 허용시간 표시 |
| 필수 컬럼 누락 | Flow 중단 |
| 미등록 판정값 | 운영 strict 모드에서 Flow 중단 |
| Case key 중복 | Flow 중단 또는 데이터 담당자 승인 시 명시적 집계 |
| Report HTML 크기 초과 | Report 생성 중단, Preview 행 축소 필요 안내 |
| Langflow 파일 저장 실패 | Flow 오류 |
| Report API 게시 실패 | `status=partial`, 채팅 요약은 반환하고 링크 실패 경고 |
| Report 링크 만료 | 서버 `410 Gone` |
| Report 저장소 용량 초과 | 신규 게시 거부 및 운영 알림 |

Report API 게시에 실패해도 Langflow 내부 파일 저장이 성공했다면 결과 자체는 보존된다. 다만 사용자용 절대 URL이 없으므로 `partial`로 표시한다.

---

## 14. 모니터링과 운영 로그

### 14.1 Snapshot Loader 지표

- 최신 완료 Snapshot 시각
- Snapshot freshness seconds
- 조회 행 수
- 조회 공정 수
- DB/API 조회시간
- 검증 오류 유형별 건수
- 미등록 판정값 건수
- 중복 Case 건수

### 14.2 Report 생성 지표

- Report 생성 성공/실패/partial 건수
- 생성 소요시간
- HTML byte 크기
- 전체 행 수와 표시 행 수
- Report API 게시시간
- Report API HTTP 오류율

### 14.3 Report 서버 지표

- `/health` 상태
- 활성 Report 개수
- 저장소 사용량
- TTL 삭제 건수
- 보기/다운로드 요청 수
- `401/403/404/410/413/500` 응답 수

### 14.4 추적 식별자

다음 식별자를 구조화 로그에 함께 기록한다.

```text
session_id
snapshot_id
report_id
rules_version
judgement_rules_version
```

원본 행, MongoDB URI, URL access token은 로그에 기록하지 않는다.

---

## 15. 테스트 및 업무 검증

### 15.1 Golden Dataset

업무 담당자가 검토한 소규모 고정 데이터를 만든다.

반드시 포함할 Case:

- 정상
- 정상(초과생산)
- Abnormal
- 생산부족
- 적정재공부족 단일원인
- CAPA부족 단일원인
- 가동율저조 단일원인
- 복합 생산부족 원인
- CAPA `생산부진1`
- CAPA `생산부진2`
- CAPA `CAPA부족`
- 장비필요
- 교체필요
- 교체불필요
- Down 장비 포함 Case

Golden Dataset별 기대 KPI, PIE CHART 건수, Radio 필터 건수, Action 문구를 함께 관리한다.

### 15.2 데이터 품질 테스트

- 45개 전체 컬럼 존재
- 판정값 허용목록 검증
- `case_key` 유일성
- `row_count` 일치
- WORK_DATE 단일성
- 숫자 컬럼 타입
- null/빈 문자열 정책
- 오래된 Snapshot 거부
- 0행 공정 거부

### 15.3 기능 테스트

- 네 개 탭 전환
- 여섯 개 PIE CHART 렌더링
- Radio 필터별 행 수
- 제품·공정 검색
- 전체 컬럼 전환
- Excel 호환 CSV 한글
- HTML 보기와 다운로드
- TTL 만료 후 `410 Gone`
- Report API 장애 시 `partial` 응답

### 15.4 성능 테스트

최소 다음 규모로 검증한다.

| 구간 | 목적 |
| --- | --- |
| 500행 | 현재 예시와 동일한 기본 동작 |
| 1,000행 | 기본 HTML 표시 상한 |
| 5,000행 | 현재 HTML 최대 행 |
| 운영 최대 예상 행 | Preview/서버 다운로드 분리 여부 결정 |
| 동시 10~50 요청 | Report 생성 및 저장소 경합 확인 |

### 15.5 업무 숫자 대조

동일 Snapshot을 기존 수작업 Excel/Report와 비교한다.

- 전체 Case
- 정상/초과
- Abnormal
- 생산부족
- 생산부족 원인
- CAPA 세부분석
- 장비필요
- 교체필요

차이가 발생하면 코드부터 수정하지 말고 먼저 집계 Grain, 중복 제품, 복합원인 우선순위를 확인한다.

---

## 16. 단계별 구현 계획

### Phase 1. 업무 기준 확정

산출물:

- Case/Product Grain 정의
- 생산부족 주원인 우선순위
- Snapshot 완료 기준
- 허용 Snapshot 최대시간
- 대상 공정 선택 방식
- 최대 예상 행 수

완료 기준:

- 업무 담당자와 개발 담당자가 같은 숫자 정의에 동의
- Golden Dataset과 기대 결과 승인

### Phase 2. 실제 Snapshot 저장

산출물:

- Snapshot 메타데이터 저장
- 판정 행 저장
- `building → complete/failed` 처리
- 필수 인덱스
- `judgement_rules_version`

완료 기준:

- 저장 중 Snapshot이 조회되지 않음
- 메타데이터 행 수와 실제 행 수 일치
- Case key 중복 없음

### Phase 3. Live Snapshot Loader

산출물:

- `LiveProductionJudgementSnapshotLoader`
- 보이는 운영 입력
- strict 데이터 검증
- `production.judgement.dataset.v1` 출력
- Loader 단위 테스트

완료 기준:

- 실제 Snapshot으로 기존 Report 생성기 실행 성공
- 누락·중복·오래된 Snapshot 테스트 통과

### Phase 4. Flow 교체 및 동기화

산출물:

- 더미 노드를 Live Loader로 교체한 Python component
- 11번 Flow JSON
- import-ready 개별 JSON
- 전체 import-ready bundle

완료 기준:

- Langflow 1.9.2 import 성공
- Custom Component source 동기화 통과
- 11개 Flow의 node template parse 통과

### Phase 5. Report 서버 운영 배포

산출물:

- 내부 Report Service
- Persistent Storage
- HTTPS/사내 인증
- access token
- health check
- 서비스 자동 시작

완료 기준:

- Langflow에서 `POST /reports` 접근 가능
- 사용자 브라우저에서 내부 HTTPS URL 접근 가능
- 만료·다운로드·재시작 테스트 통과

### Phase 6. UAT 및 운영 전환

산출물:

- 실제 Snapshot 숫자 대조표
- 성능 테스트 결과
- 장애 대응 절차
- 운영 모니터링 Dashboard
- Rollback 절차

완료 기준:

- 기존 업무 Report와 승인된 오차 기준 내 일치
- 운영 최대 행과 동시 요청 테스트 통과
- 보안 및 데이터 보관기간 승인

---

## 17. Rollback 계획

운영 전환 시 Live Loader와 더미 Loader를 동시에 연결하지 않는다. 대신 아래 두 개의 Flow export를 별도로 보관한다.

```text
realtime_production_report_flow_dummy_validation
realtime_production_report_flow_live
```

운영 장애 시 다음 순서로 대응한다.

1. Router/Workflow Registry에서 live Flow 호출을 중단한다.
2. 마지막 정상 Snapshot과 Report 서버 상태를 확인한다.
3. 판정 Job 또는 Loader 문제를 분리한다.
4. 사용자에게 마지막 Snapshot 시각과 장애 상태를 표시한다.
5. 업무 검증용 dummy Flow는 운영 데이터 대체용으로 사용하지 않는다.
6. 수정 후 Golden Dataset과 실제 Snapshot 검증을 다시 수행한다.

더미 Flow는 기능 검증용이며 실제 생산 판단의 대체 결과로 제공하면 안 된다.

---

## 18. 구현 착수 전 확정 체크리스트

### 데이터

- [ ] 실제 데이터 저장소 종류가 정해졌는가
- [ ] Database/Table/Collection 이름이 정해졌는가
- [ ] 판정 Job 완료 상태를 확인할 수 있는가
- [ ] 45개 전체 컬럼의 실제 타입이 정의되었는가
- [ ] 평균 및 최대 행 수를 알고 있는가
- [ ] Snapshot 보관기간이 정해졌는가

### 업무 규칙

- [ ] Case와 Product Grain이 확정되었는가
- [ ] 복합 생산부족 원인 우선순위가 승인되었는가
- [ ] 공정 선택이 고정형인지 채팅 조건형인지 정해졌는가
- [ ] Snapshot 최대 허용시간이 정해졌는가
- [ ] Action 문구의 담당부서와 운영 절차가 맞는가

### 인프라

- [ ] Langflow에서 Report 서버로 접근할 수 있는가
- [ ] 사용자 브라우저용 내부 HTTPS 주소가 있는가
- [ ] 사내 인증 방식이 정해졌는가
- [ ] Report 저장용 Persistent Storage가 있는가
- [ ] 서비스 자동 시작과 health monitoring이 가능한가
- [ ] URL 및 query token 로그 마스킹이 가능한가

### 다운로드

- [ ] Excel 호환 CSV로 충분한가
- [ ] 실제 XLSX가 필요한가
- [ ] HTML 표시 상한보다 전체 데이터가 클 수 있는가
- [ ] 전체 데이터 다운로드의 보관시간이 정해졌는가

---

## 19. 관련 파일

| 목적 | 파일 |
| --- | --- |
| 현재 더미 데이터 생성기 | `langflow_components/realtime_production_report_flow/00_dummy_production_judgement_data.py` |
| 현재 Report 생성기 | `langflow_components/realtime_production_report_flow/01_realtime_production_report_builder.py` |
| API 종료 어댑터 | `langflow_components/realtime_production_report_flow/02_realtime_production_report_api_terminal.py` |
| Flow 연결 가이드 | `langflow_components/realtime_production_report_flow/CONNECTION_GUIDE.md` |
| 11번 Flow JSON | `flow_exports/realtime_production_report_flow_v5_standalone.json` |
| 통합 Report 서버 | `tools/data_ref_download_server.py` |
| 다운로드 서버 운영 가이드 | `docs/DATA_RESULT_DOWNLOAD_SERVER_GUIDE.md` |
| HTML 링크 가이드 | `docs/HTML_REPORT_LINK_GUIDE.md` |

---

## 20. 최종 권장안

최초 운영 버전은 아래 범위로 제한한다.

```text
고정 공정형
+ 최신 complete Snapshot
+ Snapshot 최대 허용시간 검증
+ 45개 전체 컬럼 strict 검증
+ 단일 Report Service와 Persistent Volume
+ 사내 HTTPS 인증
+ HTML 최대 1,000행 Preview
+ 전체 CSV/XLSX는 서버 측 별도 다운로드
```

이 구성으로 운영 안정성을 먼저 확보한 뒤 다음 단계에서 채팅 기반 공정 선택, 정기 선생성, 다중 Replica, 대용량 XLSX 생성 기능을 확장한다.
