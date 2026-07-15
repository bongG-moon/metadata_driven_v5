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

`retrieval_jobs`의 LLM 소유 필드는 아래로 제한합니다.

```json
{
  "dataset_key": "wip_today",
  "source_alias": "wip_data",
  "required_params": {"DATE": "20260710"},
  "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1"]}}
}
```

LLM이 `source_type`, `source_config`, SQL 또는 URL을 출력해도 `04A 신뢰 카탈로그 조회 작업 구성기`가 버립니다.

각 retrieval job의 `required_params`는 그 job만으로 바로 조회할 수 있는 완성된 값이어야 합니다. 하나의 날짜·PLANT·FAB·SHIFT 조건이 여러 job에 공통이면 해당 값을 각 job에 반복하고, `어제 재공과 오늘 생산량`처럼 조건 범위가 다르면 job별로 서로 다른 값을 둡니다. Hydrator는 한 job의 값을 다른 job으로 전파하지 않으며 누락은 `missing_catalog_required_params` 경고로 남깁니다.

dataset 선택은 요청 grain을 우선합니다. LOT·랏·로트·LOT_ID·LOT 상태처럼 LOT 단위 근거가 없는 일반 재공/WIP 질문은 집계 WIP dataset을 사용하고 `lot_status`로 대체하지 않습니다.

dummy/live는 `04A.retrieval_mode` 한 곳에서만 설정합니다. `04A`는 정규화한 값을 `request.retrieval_mode`에 기록하고 `07 데이터 조회 작업 라우터`는 이 값만 읽습니다.

### Trusted hydration 이후

Active table catalog에서 다음을 주입합니다.

- `source_type`
- sanitized `source_config`
- `required_param_names`
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
- 제품 그룹/제품군 등록 정보는 `product_terms`만 선택하며, 별칭·기본 조건·`condition_by_family`·`condition_by_dataset`을 등록값 그대로 표로 만듭니다.
- 제품 집계 방법은 `product_key_columns`의 제품 식별 컬럼과 관련 `analysis_recipes`의 `grain_policy`/`group_by`를 분리해 설명합니다.
- 제품 그룹과 제품 집계 표는 context를 authoritative 근거로 결정론적으로 생성하므로 추가 LLM 호출을 생략합니다. 실제 제품별 생산량처럼 값 계산이 필요한 질문은 계속 `data_analysis_redirect`입니다.

## 7. API Router

- 하위 Flow 요청은 `input_value`, `input_type`, `output_type`, `session_id`만 전달합니다.
- API caller의 `session_id`는 Langflow graph가 부모 실행 세션으로 자동 주입합니다. `Chat Input.message`는 Smart Router에만 연결하고 caller `session_source` fan-out은 만들지 않습니다.
- 사용자 표시는 `message`, 운영 상태는 `status_data`로 분리합니다.
- `status_data`에는 route, HTTP/downstream status, duration, errors만 포함하고 하위 payload를 복제하지 않습니다.
- API key는 Secret input/Global Variable을 사용합니다.

## 8. Route V3 Orchestration

Route V3 Tool의 공개 입력은 node ID가 없는 고정 계약입니다.

```json
{
  "question": "해당 LOT의 HOLD 이력을 조회해줘",
  "upstream_result_ref": "result:session-id:uuid"
}
```

- 첫 호출 또는 서로 독립적인 호출은 `upstream_result_ref`를 비웁니다.
- 종속 호출은 직전 Tool이 반환한 ref를 수정 없이 전달합니다.
- 하위 Tool 호출은 요청당 최대 4회이며 Agent `max_iterations=5`, `return_direct=false`를 사용합니다.
- Agent observation은 `route_v3.tool_result.v1`의 `status`, `summary`, `result_ref`, `result_ref_meta`, `entity_ids`, `handoff_usable`, 제한된 오류만 포함합니다.
- 전체 rows, source payload, SQL, trace, intent plan, pandas 코드는 Agent observation에 넣지 않습니다.
- `status=error` 또는 `handoff_usable=false`인 결과에 의존하는 후속 호출은 중단합니다.

Data Analysis에서 명시적 ref를 받으면 같은 `session_id`의 `datagov.agent_v4_result_store` 문서만 읽고, 저장된 전체 `payload.result_rows`를 `runtime_sources.upstream_result`로 복원합니다. 다음 조회의 파라미터는 Table Catalog의 `source_config.upstream_bindings` 선언으로만 결정합니다.

```json
{
  "upstream_bindings": [
    {
      "entity_type": "lot",
      "source_alias": "upstream_result",
      "source_column": "LOT_ID",
      "target_param": "LOT_ID",
      "operator": "in",
      "max_values": 200
    }
  ]
}
```

binding 누락·모호성·기존 파라미터 충돌·식별자 상한 초과는 broad query fallback 없이 fail-closed 처리합니다. 일반 단일 분석과 기존 세션 후속 질문은 명시적 ref가 없으므로 이 경로를 타지 않습니다.

## 9. Route V4 Workflow Orchestration

계획 모델 출력은 실행 전에 `workflow.plan.v1`로 정규화합니다.

```json
{
  "contract_version": "workflow.plan.v1",
  "workflow_run_id": "workflow:uuid",
  "workflow_key": "wb_daily_production_metadata",
  "title": "WB 당일 생산량과 공정 그룹 정의 조회",
  "user_question": "오늘 WB 생산량과 등록 정의를 알려줘",
  "max_steps": 4,
  "steps": [
    {
      "step_index": 1,
      "step_id": "production",
      "tool_name": "run_data_analysis",
      "question": "오늘 WB 공정 생산량을 조회해.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    }
  ]
}
```

- 단계는 최대 4개이며 Loop 시작 전 전체 계획을 검증합니다.
- `depends_on`은 앞 단계 ID만 참조할 수 있습니다.
- `handoff=result_ref`는 정확히 하나의 dependency가 있을 때만 허용합니다.
- `tool_name`은 Route V4 화면의 허용 Tool 목록과 정확히 일치해야 합니다.
- 계획 오류가 있으면 steps를 비우고 Tool을 호출하지 않습니다.

각 Loop iteration의 결과는 `workflow.step_result.v1`로 제한합니다.

```json
{
  "contract_version": "workflow.step_result.v1",
  "workflow_run_id": "workflow:uuid",
  "workflow_key": "wb_daily_production_metadata",
  "step_index": 1,
  "step_id": "production",
  "tool_name": "run_data_analysis",
  "status": "ok",
  "summary": "오늘 WB 생산량은 ...입니다.",
  "result_ref": "result:session-id:uuid",
  "result_ref_meta": {},
  "entity_ids": [],
  "warnings": [],
  "errors": []
}
```

Loop 상태와 마지막 모델에는 원본 rows, SQL, source payload, pandas code, trace를 넣지 않습니다. `result_ref`는 다음 단계가 실제 상위 결과를 요구할 때만 Tool 입력으로 전달합니다.

최종 API 응답은 다음 공통 envelope을 사용합니다.

```json
{
  "response_type": "workflow_orchestration",
  "status": "ok",
  "message": "최종 사용자 답변",
  "workflow": {
    "contract_version": "workflow.final_context.v1",
    "workflow_key": "wb_daily_production_metadata",
    "execution_status": "ok",
    "step_count": 2,
    "steps": []
  },
  "errors": []
}
```

부모 Route V4만 질문과 최종 답변을 저장합니다. child Chat Input/Output은 이름 기반 Tool의 runtime tweak로 메시지 저장을 끕니다.

## 10. Workflow Skill Saving

Workflow Skill 저장 문서는 `datagov.agent_v4_workflow_skills`에 다음 형태로 기록합니다.

```json
{
  "_id": "workflow:wb_daily_production_metadata",
  "section": "workflow_skills",
  "key": "wb_daily_production_metadata",
  "status": "active",
  "payload": {
    "display_name": "WB 생산량과 메타데이터 조회",
    "description": "생산량 조회 후 등록 정의를 조회합니다.",
    "aliases": [],
    "intent_examples": [],
    "keywords": [],
    "excluded_keywords": [],
    "priority": 100,
    "steps": []
  },
  "updated_at": "ISO-8601"
}
```

- 기본값은 `dry_run=true`이며 Writer만 실제 MongoDB 변경 권한을 가집니다.
- 저장 전 최대 4단계, 허용 Tool 5종, 앞 단계 dependency, `result_ref` producer/consumer, 단계 질문 4,000자, 전체 payload 32KB를 Python으로 다시 검증합니다.
- `replace`는 유사 identity가 정확히 1건이면 기존 canonical `_id`/key를 유지해 교체하고, 0건이면 신규 저장하며, 복수이면 차단합니다.
- `skip`, `merge`, `replace`, `create_new` 외에 대기형 `ask` 모드는 사용하지 않습니다.
- `revision`, `request_id`, `idempotency` 필드는 추가하지 않습니다.
- 응답에는 전체 MongoDB 문서나 LLM 원문 JSON을 복제하지 않고 Workflow key, 단계 요약, operation과 오류만 남깁니다.
