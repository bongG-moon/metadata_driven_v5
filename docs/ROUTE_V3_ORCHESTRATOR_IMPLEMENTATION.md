# Route V3 최대 4회 연계 Orchestrator 구현

## 목적과 기존 Router 구분

- 06 API Router는 단일 분기를 가장 빠르게 호출하는 운영 기본 경로다.
- 07 Route V2는 기본 Langflow Agent가 Tool 하나를 선택하고 `return_direct=true`로 하위 답변을 그대로 반환한다.
- 08 Route V3는 앞 단계 결과가 다음 단계 입력이 되는 질문을 위해 최대 4개의 Tool을 순차 호출하고, 마지막에만 기본 Langflow Agent가 답변을 한 번 생성한다.

V2는 변경하지 않았다. 단일 Flow로 충분한 질문은 V2 또는 API Router를 사용하고, 여러 분석 결과를 실제 데이터 단위로 연결해야 할 때만 V3를 사용한다.

## 부모 Flow 구조

```text
Chat Input
  -> Langflow 기본 Agent <- 이름 기반 Cached Flow Tool 5개
  -> Chat Output
```

- Agent: Langflow 기본 `Agent`
- `max_iterations=5`
- 시스템 프롬프트 Tool 호출 상한: 4회
- Tool: `cache_flow=true`, `return_direct=false`
- 부모 Chat Input/Output만 메시지 저장
- child Chat Input/Output은 runtime tweak로 저장 차단
- Tool 목록 생성 시 child graph를 열지 않고, 선택된 Tool만 lazy-load

`max_iterations=5`는 네 번의 Tool action 뒤 최종 답변을 만들 수 있는 실행 여유다. Tool 호출 4회 제한은 기본 Agent를 유지하기 위해 시스템 프롬프트 정책으로 적용하며, 별도 custom Agent나 전역 호출 카운터는 추가하지 않았다.

## Tool 공개 입력

모델에는 import 후 바뀌는 node ID를 노출하지 않는다.

```json
{
  "question": "해당 LOT의 HOLD 이력을 조회해줘",
  "upstream_result_ref": "result:session-id:uuid"
}
```

- `question`: 필수
- `upstream_result_ref`: 선택
- 첫 Tool과 서로 독립적인 Tool 호출에서는 ref를 비운다.
- 종속 Tool은 직전 결과의 ref 문자열을 수정 없이 전달한다.
- ref 입력을 지원하지 않는 Tool에 ref를 넣으면 즉시 오류로 종료한다.

## Tool 결과 계약

하위 Flow의 terminal `api_response`를 우선 선택하고 다음 compact 계약으로 변환한다.

```json
{
  "contract_version": "route_v3.tool_result.v1",
  "status": "ok",
  "tool_name": "run_data_analysis",
  "summary": "이상 LOT 12건을 확인했습니다.",
  "result_ref": "result:session-id:uuid",
  "result_ref_meta": {
    "role": "analysis_result",
    "row_count": 12,
    "columns": ["LOT_ID", "OPER_NAME"]
  },
  "entity_ids": [
    {
      "entity_type": "lot",
      "column": "LOT_ID",
      "values": ["LOT001", "LOT002"],
      "complete": false
    }
  ],
  "handoff_usable": true,
  "warnings": [],
  "errors": []
}
```

- 전체 rows, runtime source, SQL, trace, intent plan, pandas 코드는 Agent에 전달하지 않는다.
- summary 최대 2,000자, entity preview 컬럼당 최대 50개, 이슈 최대 5개, 전체 observation 약 8KB를 적용한다.
- ID preview는 설명·검증용이다. 후속 Flow의 전체 입력은 반드시 `result_ref`로 복원한다.
- `status=error` 또는 `handoff_usable=false`이면 해당 결과에 의존하는 후속 Tool을 실행하지 않는다.

## Data Analysis 연계 처리

```text
00 분석 요청 로더
-> 04A 신뢰 카탈로그 조회 작업 구성기
-> 05 MongoDB 이전 결과 로더
-> 05A 상위 결과 파라미터 바인더
-> 06 조회 작업 검증기
-> source별 조회
-> 13/14 alias 병합
-> pandas 분석
-> 23 MongoDB 결과 저장소
```

1. `00`은 명시적 ref가 있을 때만 `orchestration` 영역을 만든다.
2. `05`는 같은 `session_id`의 `datagov.agent_v4_result_store` 문서를 읽는다.
3. 저장 결과가 완전할 때만 전체 `payload.result_rows`를 `runtime_sources.upstream_result`로 복원한다.
4. `05A`는 Table Catalog가 선언한 `source_config.upstream_bindings`만 사용한다.
5. `13/14`는 `upstream_result`와 새 조회 source를 alias 기준으로 함께 보존한다.
6. 현재 분석 결과는 새 ref로 저장하므로 상위 결과 문서를 덮어쓰지 않는다.

일반 단일 질문과 기존 세션 후속 질문에는 명시적 ref가 없으므로 이 경로가 활성화되지 않는다.

## Table Catalog binding

```json
{
  "source_config": {
    "query_template": "SELECT * FROM HOLD_HISTORY WHERE LOT_ID IN ({LOT_ID})",
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
}
```

binding은 특정 LOT/HOLD fallback 코드가 아니라 dataset metadata다. `entity_type`, `source_column`, `target_param`은 필수이며 operator는 `in` 또는 `eq`, max values는 1~10000만 허용한다. 누락·모호성·기존 값 충돌·상한 초과 시 모든 관련 job을 차단해 broad query를 방지한다.

## 오류 정책

- 다른 세션 ref: `upstream_session_mismatch`
- ref 미존재: `upstream_result_not_found`
- 빈/잘린 저장 결과: `upstream_result_empty` 또는 `upstream_result_incomplete`
- binding 없음: `upstream_binding_missing`
- 식별자 컬럼 없음: `upstream_source_column_missing`
- 값 상한 초과: `upstream_entity_limit_exceeded`
- child Flow 실패: Tool contract의 `status=error`

인프라·ref·binding 오류에 다른 Tool fallback을 호출하지 않고, 성공한 단계와 실패한 단계를 마지막 답변에서 구분한다.

## 이상 LOT Flow 추가 방법

현재 bundle에는 별도 이상 LOT 전용 Flow가 없으므로 Data Analysis가 해당 분석을 수행할 수 있다. 이후 전용 Flow를 추가할 때는 Route V3 spec에 다음 capability를 선언한다.

```text
accepts_upstream_result_ref=false
can_produce_result_ref=true
entity_id_columns=LOT_ID
return_direct=false
```

전용 Flow도 동일한 MongoDB 결과 저장 계약과 terminal `api_response.data_refs`를 제공해야 한다. 그러면 `이상 LOT 조회 -> Data Analysis HOLD 이력` 순차 호출을 Router 코드 변경 없이 같은 handoff 계약으로 연결할 수 있다.

## 검증 결과

- 전체 pytest: 265개 통과
- 대표 dummy 질문: 23/23 통과
- source/JSON/bundle 동기화: 각 계층 80/80 custom node, 실제 원본 68개, 누락 0
- 한글 함수 설명: Python 69개, 함수 1150/1150
- Langflow 1.8.2 / LFX 0.3.4 template parse: 123/123
- 격리 Langflow 1.8.2 개별 JSON import: 8/8 HTTP 201
- 통합 `00` 단일 JSON import: HTTP 201, 8개 Flow 생성

실제 Tool 선택·4단계 순차 호출 품질은 운영의 Tool-calling 지원 모델과 실제 MongoDB/원천 데이터가 필요하므로, 배포 후 같은 session으로 1단계·2단계·4단계 질문을 각각 검증해야 한다.
