# Data Retrieval Sources

이 프로젝트의 기본 데이터 조회 단위는 `metadata/table_catalog.json`의 dataset입니다.
샘플 JSON 파일을 운영 조회 소스로 보지 않고, 각 dataset의 `source_type`과 `source_config`를 기준으로 조회 방식을 결정합니다.

## Source Types

retriever는 기존 구현에서 사용하던 4개 방식과 dummy 방식을 모두 지원합니다. 현재 seed catalog는 예전 table catalog raw input을 기준으로 Oracle/Goodocs dataset을 기본 등록합니다.

| source_type | 용도 | 현재 예시 dataset |
| --- | --- | --- |
| `oracle` | 생산, 재공, lot, hold 이력, 설비 현황, UPH처럼 SQL로 조회하는 MES/RPT/GMS 계열 데이터 | `production_today`, `production`, `wip_today`, `wip`, `lot_status`, `hold_history`, `equipment_status`, `capacity` |
| `h_api` | API 형태로 조회하는 이력/상세 데이터 | 별도 h_api catalog 등록 시 사용 |
| `datalake` | Datalake/SmallData/StarRocks 계열 분석 데이터 | 별도 datalake catalog 등록 시 사용 |
| `goodocs` | Goodocs 문서/시트 기반 목표, 계획 데이터 | `target` |
| `dummy` | 로컬 개발과 회귀검증용 deterministic fixture | 필요 시 임시 dataset |

## Runtime Behavior

Python reference runtime은 `reference_runtime/retrieval.py`에서 source별 분기를 수행합니다.

- Langflow 기본값: `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode=dummy`
- 더미 모드에서는 실제 Oracle/API/Datalake/Goodocs를 호출하지 않고 deterministic dummy rows를 사용합니다.
- 이때도 `source_type`, `source_config`, `source_execution`, `used_dummy_data`를 남기므로 Langflow wiring과 pandas 분석 scope를 실제 구조처럼 검증할 수 있습니다.
- 운영 연결을 시험하려면 `04A.retrieval_mode` 하나만 `live`로 바꾸고 source별 credential/config를 채웁니다. `07`은 payload에 기록된 동일 모드를 읽으며 별도 설정이 없습니다.

## Environment Keys

```dotenv
ORACLE_CONFIG_JSON=
H_API_BASE_URL=
H_API_TOKEN=
DATALAKE_MODULE_NAME=lakes
DATALAKE_CLASS_NAME=LakeHouse
LAKEHOUSE_USER_ID=
LAKEHOUSE_TOKEN=
LAKEHOUSE_S3_ACCESS_KEY=
LAKEHOUSE_S3_SECRET_KEY=
GOODOCS_USER_ID=
GOODOCS_TOKEN_SOURCE=
GOODOCS_TOKEN_KEY=
SOURCE_FETCH_LIMIT=5000
```

`ORACLE_CONFIG_JSON` 또는 `09 Oracle 쿼리 조회기`의 `Oracle 설정/TNS` 입력은 예를 들어 아래 JSON 형태를 기대합니다.

```json
{
  "PNT_RPT": {
    "user": "USER_ID",
    "password": "PASSWORD",
    "dsn": "(DESCRIPTION=...)"
  }
}
```

TNS block을 직접 붙여 넣을 때는 db_key를 앞에 붙인 named block 형태도 사용할 수 있습니다.

```text
PNT_RPT:
(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=...)(PORT=...))(CONNECT_DATA=(SERVICE_NAME=...)))
```

여러 Oracle DB를 쓰는 경우 `PNT_RPT`, `GMS_DB`처럼 table catalog의 `source_config.db_key`와 같은 이름으로 나누어 입력합니다.

Datalake Langflow component는 LakeHouse 방식으로 실행합니다. 입력으로 받은 `LAKEHOUSE_USER_ID`, `LAKEHOUSE_TOKEN`, `LAKEHOUSE_S3_ACCESS_KEY`, `LAKEHOUSE_S3_SECRET_KEY`를 환경변수에 세팅한 뒤 `lakes.LakeHouse(real_user_id=...)`, `ensure_running(cluster_type="starrocks")`, `auto_run_sync_paragraph(code=sql)`, `get_rst()` 순서로 결과를 읽습니다.

H-API Langflow component는 `source_config.api_url/url/endpoint`, `method`, `headers`, `params/query_params`, `body/payload`, `response_path`를 사용해 HTTP 요청을 실행합니다. `{DATE}` 같은 템플릿 변수는 `retrieval_jobs[].required_params` 또는 `params` 값으로 치환합니다.

Goodocs Langflow component는 기존 운영 환경과 같은 `Goodocs(auth)` class 계약으로 조회합니다. 실제 운영 환경에서는 `12_goodocs_retriever.py`의 `Goodocs` class를 운영용 class로 교체하면 됩니다.

운영 class는 아래 형태를 유지하면 됩니다.

```python
class Goodocs:
    def __init__(self, auth):
        self.auth = auth

    def read_all(self):
        ...

    # sheet_name 단위 조회가 가능한 경우 선택 구현
    def read_sheet(self, sheet_name):
        ...
```

`auth`에는 `USER_ID`, `DOC_ID`, `TOKEN_SOURCE`, `TOKEN_KEY`가 들어가며, `sheet_name`이 있으면 `SHEET_NAME`도 함께 전달됩니다. 반환값은 pandas DataFrame, `list[dict]`, 또는 rows/data/items/result/results/records를 포함한 dict면 됩니다.

## Langflow Components

`langflow_components/data_analysis_flow/`에는 data analysis flow에서 바로 연결하는 source retriever custom component가 들어 있습니다.

- `06_retrieval_job_validator.py`: `intent_plan.retrieval_jobs` 구조 검증
- `07_retrieval_job_router.py`: source type별 조회 branch 분기
- `08_dummy_data_retriever.py`: `04A.retrieval_mode=dummy`일 때 deterministic dummy data 조회
- `09_oracle_query_retriever.py`: Oracle source job 처리
- `10_h_api_retriever.py`: H-API source job 처리
- `11_datalake_retriever.py`: Datalake source job 처리
- `12_goodocs_retriever.py`: Goodocs source job 처리
- `13_source_retrieval_merger.py`: 여러 source 결과를 하나의 retrieval payload로 병합
- `14_retrieval_payload_adapter.py`: pandas 분석용 `runtime_sources` 구성

각 파일은 Langflow custom component에 하나씩 붙여 넣어도 동작하도록 sibling import 없이 작성했습니다.
불필요한 port를 늘리지 않기 위해 입력은 payload와 source별 credential/config 정도만 둡니다.

## Dummy Data Coverage

dummy data는 아래 질문군을 검증할 수 있도록 여러 공정, 제품, lot, 설비, 목표 데이터를 생성합니다.

- DA/WB 공정별 재공 상위 제품과 해당 제품 생산량 join
- LPDDR5, HBM, TSV, MCP_NO, TSV_DIE_TYP 같은 제품 조건 필터
- lot count/nunique, waiting/running/hold 상태 집계
- 특정 lot의 hold 이력
- 후속 질문에서 이전 결과의 제품 grain을 이용한 설비 조회
- 목표 대비 생산 저조, 달성률, balance 계산
- 전체 재공처럼 이전 scope를 reset해야 하는 질문

`sample_data/` 폴더는 과거 JSON fixture와 비교하거나 업로드 도구를 실험할 때만 쓰는 보조 자료입니다.
현재 reference runtime의 기본 조회 경로는 `sample_data/*.json`이 아닙니다.
