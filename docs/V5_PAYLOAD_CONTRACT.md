# v5 Payload Contract

## 1. 단일 소유 원칙

| 정보 | canonical 위치 | 다른 위치의 정책 |
| --- | --- | --- |
| 최종 row | `data.rows` | `analysis`, `answer_sections`에 복제하지 않음 |
| 컬럼/건수 | `data.columns`, `data.row_count` | `analysis`에는 실행 요약만 유지 |
| 실행 pandas 코드 | `trace.inspection.pandas_execution.generated_code` | `analysis_code`, `effective_code_with_helpers` 제거 |
| 중간 산출물/helper 결과 | `analysis.step_outputs`, `analysis.function_case_results` | trace에 같은 배열을 복제하지 않음 |
| 사용자 표시 메시지 | 최종 API의 `message` | `answer_message`, `display_message` alias 제거 |
| 실행 데이터 형태 | 최종 API의 `data_mode` | `dummy` 또는 `live` |
| 표 렌더링 row 출처 | `answer_sections.result_table.row_source=data.rows` | section 내부에 rows/display_rows를 넣지 않음 |

## 2. 단계별 payload

### Intent LLM 입력

LLM에는 다음만 전달합니다.

- 현재 질문과 compact state
- 질문 관련 metadata 후보
- runtime helper 이름/선택 정책
- 출력 schema

Metadata 후보의 기본 정책은 도메인 관련 항목 최대 10건, 테이블 관련 후보 최소 5건·최대 10건, 메인 필터 전체이며 최종 compact JSON은 32KB 이내입니다. `query_template`, SQL, endpoint, header, token, password 같은 실행/비밀 설정은 제거합니다. 바이트 압박 시 관련도 낮은 도메인부터 줄이고 테이블 최소 후보와 메인 필터 전체를 우선 보존합니다.

### Intent LLM 출력

`retrieval_jobs`의 LLM 소유 필드는 아래로 제한합니다. job마다 적용 대상에 맞는 필수 파라미터 값을 직접 작성합니다.

```json
{
  "dataset_key": "wip_today",
  "source_alias": "wip_data",
  "required_params": {"DATE": "20260710"},
  "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1"]}}
}
```

같은 필수 파라미터 값이 해당 파라미터를 요구하는 모든 job에 공통 적용된다는 것이 질문에서 명확할 때만 plan 수준의 `shared_required_params`를 사용할 수 있습니다.

```json
{
  "shared_required_params": {"DATE": "20260710"},
  "retrieval_jobs": []
}
```

`어제 재공과 오늘 생산량`처럼 대상별 값이 다르면 `shared_required_params`를 사용하지 않고 각 job의 `required_params.DATE`를 별도로 작성합니다. 이 scope 규칙은 `DATE`뿐 아니라 `PLANT`, `FAB`, `SHIFT` 등 모든 catalog 필수 파라미터에 동일하게 적용합니다. `04A`는 질문의 첫 날짜나 다른 job의 값을 공통값으로 추정하지 않습니다.

LLM이 `source_type`, `source_config`, SQL 또는 URL을 출력해도 `04A 신뢰 카탈로그 조회 작업 구성기`가 버립니다.

dummy/live는 `04A.retrieval_mode` 한 곳에서만 설정합니다. `04A`는 정규화한 값을 `request.retrieval_mode`에 기록하고 `07 데이터 조회 작업 라우터`는 이 값만 읽습니다.

### Trusted hydration 이후

Active table catalog에서 다음을 주입합니다.

- `source_type`
- sanitized `source_config`
- `required_param_names`
- catalog key로 정규화된 job별 `required_params`
- `trusted_catalog=true`
- `catalog_ref`

`live` 모드에서 모르는 `dataset_key`는 job에서 제외하고 error를 기록합니다. `dummy` 모드에서는 `source_type=dummy`, `dummy_only=true`로 표시해 검증기를 통과시킨 뒤 더미 조회기로 전달합니다.

### Retrieval branch

각 retriever에 전달하는 객체는 전체 main payload가 아닙니다.

```json
{
  "retrieval_job_bundle": {
    "source_type": "oracle",
    "jobs": [],
    "retrieval_mode": "live",
    "live_source_retrieval": true
  },
  "request_context": {
    "session_id": "...",
    "reference_date": "20260710"
  },
  "routing_trace": {
    "input_job_count": 1,
    "selected_job_count": 1,
    "source_type": "oracle",
    "retrieval_mode": "live"
  }
}
```

state, 전체 intent plan, 이전 runtime rows는 분기로 복제하지 않습니다.

### Pandas 실행과 Repair

- `trace.inspection.pandas_execution.generated_code`는 import 정규화와 filter preamble 적용 후 실제 실행한 코드입니다.
- 정확한 단독 구문 `import pandas as pd`, `import numpy as np`는 실행하지 않고 제거합니다. `pd`와 제한형 `np` 계산 namespace는 executor가 주입합니다.
- 제거 내역은 `trace.inspection.pandas_execution.safe_import_normalization`에 기록합니다.
- 다른 import/import-from/혼합 import와 파일·네트워크 I/O attribute는 `unsafe_code`로 차단합니다.
- 실패 시 원본 LLM 코드는 `llm_generated_code`, 실제 실행 시도 코드는 `generated_code`로 Repair Prompt에 전달합니다. 성공 payload에는 같은 코드를 여러 alias로 복제하지 않습니다.
- Repair 감사 결과는 `trace.inspection.pandas_repair`의 `attempted`, `llm_called`, `selected`, `reason`, `initial_error`, `retry_error`, `repair_error`에 기록합니다.

## 3. 최종 API envelope

```json
{
  "response_type": "data_analysis",
  "status": "ok",
  "message": "사용자 표시용 Markdown",
  "data_mode": "dummy",
  "answer_sections": {},
  "request": {},
  "intent_plan": {},
  "analysis": {},
  "data": {"columns": [], "rows": [], "row_count": 0},
  "data_refs": [],
  "state": {},
  "trace": {}
}
```

`runtime_sources`, `_runtime_rows_by_alias`, `_full_result_rows`, `_runtime_result_rows`는 API envelope에서 제거합니다.

## 4. 보안 경계

- Intent LLM 출력은 source 실행 설정의 신뢰 원천이 아닙니다.
- Source 설정의 신뢰 원천은 active table catalog입니다.
- secret key는 trusted catalog hydration에서도 제거하며 실제 인증값은 retriever 입력 또는 환경변수로 제공합니다.
- dummy 결과는 답변 prompt와 API의 `data_mode`에서 명시합니다.
- pandas executor는 `__import__`를 제공하지 않으며 허용된 두 import 문장도 실제 import 대신 정규화합니다. 다만 pandas 코드를 같은 프로세스에서 실행하므로 OS 수준 보안 경계가 필요하면 별도 제한 프로세스/컨테이너를 사용해야 합니다.

## 5. Metadata Saving

- 기존 전체 metadata 목록은 request payload에 싣지 않습니다.
- 추출된 후보 key만 `05 동일 Key 조회기`가 MongoDB에서 조회합니다.
- Dry Run과 실저장은 추출 LLM 1회 뒤 결정론 Writer 검수를 사용하며 별도 review LLM은 호출하지 않습니다.
- standalone에서 대기·재개할 수 없는 `ask`는 제거했고 입력되면 `skip`으로 정규화합니다. `skip`, `merge`, `replace`, `create_new`는 writer에서 서로 다른 동작입니다.
- Domain `replace`는 같은 section의 exact key 또는 유일한 normalized key/alias/display_name match를 기존 canonical target으로 사용합니다. 유사 항목이 없으면 신규 저장하며, 복수 target으로 모호하면 저장하지 않습니다.
- Domain `operation_by_key`는 `operation`, `target_key`, `target_id`를 제공하고 후보 key가 canonical target과 다르면 `requested_key`와 `match_type`도 제공합니다.
- API 표시 필드는 `message`, 표 행은 `data.rows`가 canonical입니다.
- 기존 MongoDB identity/status/payload/source_config schema와 컬렉션명은 유지합니다.
- revision, request ID, idempotency 필드는 추가하지 않습니다.

## 6. Metadata QA

- loader는 identity, `status`, `payload` projection만 읽습니다.
- `available_sources`는 compact candidate rows만 LLM에 전달하며 SQL을 포함하지 않습니다.
- `dataset_sql`만 선택 dataset의 `query_template`을 전달합니다.
- 기본 상한은 50건, 65,536 bytes입니다.
- 표 행은 `data.rows`에만 두고 `answer_sections.detail_table.row_source=data.rows`로 참조합니다.
- `response_type=metadata_qa`, `direct_response_ready=true`를 유지합니다.

## 7. API Router

- 하위 Flow 요청은 `input_value`, `input_type`, `output_type`, `session_id`만 전달합니다.
- API caller의 `session_id`는 Langflow graph가 부모 실행 세션으로 자동 주입합니다. `Chat Input.message`는 Smart Router에만 연결하고 caller `session_source` fan-out은 만들지 않습니다.
- 사용자 표시는 `message`, 운영 상태는 `status_data`로 분리합니다.
- `status_data`에는 route, HTTP/downstream status, duration, errors만 포함하고 하위 payload를 복제하지 않습니다.
- API key는 Secret input/Global Variable을 사용합니다.
