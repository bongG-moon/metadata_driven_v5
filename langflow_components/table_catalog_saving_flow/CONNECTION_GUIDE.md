# Table Catalog Saving Flow 연결 가이드

권장 import 파일은 `flow_exports/table_catalog_saving_flow_v5_standalone.json`이다. 기존 MongoDB 문서 형식과 `agent_v4_table_catalog_items` 컬렉션은 유지한다.
추출 prompt는 외부 Markdown을 Langflow Prompt Template 노드에 넣고 Langflow Agent/LLM에 연결한다.

## 최적화된 경로

```text
Chat Input -> 00 Request Loader -> 03 Variables -> Saving Prompt -> Extraction Agent
Request + Agent -> 04 Normalizer -> 05 동일 Key 조회기
00 Existing Loader ---------------------> 05 동일 Key 조회기
05 -> 07 단일 Writer -> 08 단일 Response -> 09 Message -> Chat Output 1개
                                      \-> 10 API
```

- Existing Loader는 기존 전체 문서에서 `registration_trace`만 제외해 Matcher에 직접 전달한다. Request/LLM payload에는 싣지 않는다.
- 로더 제한 밖 후보는 `05`가 해당 `dataset_key`를 MongoDB에서 정확 조회하므로 중복을 놓치지 않는다.
- Dry Run과 Live 모두 metadata 추출 LLM 1회만 호출하며, Writer가 source config·SQL·credential을 결정론적으로 검증한다.
- 그래프 분기가 없으므로 Playground용 Chat Output은 하나다.

## 중복 처리

- `skip`(기본값): 기존 문서를 유지하고 해당 문서 쓰기 생략
- `merge`: 기존 문서와 새 후보를 재귀 병합
- `replace`: 같은 `_id`의 문서를 새 후보로 교체
- `create_new`: `<dataset_key>_copy`, `<dataset_key>_copy_2` 순서로 새 key 생성

## 안전 규칙

- `source_config`는 허용 필드만 저장한다.
- password, authorization, 실제 token/API key 등 credential key는 저장 전에 차단한다.
- `token_source`, `token_key`, `db_key`, `doc_id` 같은 참조 필드는 유지할 수 있다.
- SQL은 축약 없이 보존하되 Oracle/Datalake는 `query_template`이 필수다.
- 연계 조회용 `source_config.upstream_bindings`는 사용자가 식별자 관계를 명시한 경우에만 저장한다. 각 항목은 `entity_type`, `source_column`, `target_param`, `operator(in|eq)`, `max_values(1~10000)` 계약을 지켜야 한다.
- 등록 원문은 같은 `registration_trace.raw_text` 위치에 secret 제거 후 최대 2,000자로 저장한다.
- `revision`, `request_id`, idempotency 필드는 추가하지 않는다.
- standalone Langflow에는 실행을 저장한 채 사용자 결정을 기다렸다 재개하는 HITL 상태가 없으므로 `ask` 모드는 제공하지 않는다.

## Workflow 연계 조회 예시

```json
{
  "source_config": {
    "query_template": "SELECT LOT_ID, HOLD_TM, HOLD_DESC FROM HOLD_HISTORY WHERE LOT_ID IN ({LOT_ID})",
    "upstream_bindings": [
      {
        "entity_type": "lot",
        "source_column": "LOT_ID",
        "target_param": "LOT_ID",
        "operator": "in",
        "max_values": 200
      }
    ]
  }
}
```

이 규칙은 상위 결과 전체를 MongoDB `result_ref`로 복원한 뒤 적용한다. 자연어 요약이나 ID preview를 SQL 파라미터로 사용하지 않으며, binding이 없거나 충돌하면 조회를 차단한다.

Playground는 `09.message`, Web/API는 `10.api_response`를 사용한다. API 응답의 canonical 표시 필드는 `message`이며 표 행은 `data.rows`에만 둔다.
