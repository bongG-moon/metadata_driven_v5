# Route Flow V3 연결 가이드

Route V3는 기존 Route V2를 대체하지 않는다. V2는 단일 Flow를 빠르게 직접 반환하고, V3는 최대 4개의 하위 Flow를 순차적으로 실행한 뒤 Agent가 최종 답변을 한 번만 생성한다.

## 부모 Flow 구성

```text
Chat Input.message -> 기본 Langflow Agent.input_value
Orchestrated Flow Tool N종.component_as_tool -> Agent.tools
Agent.response -> Chat Output.input_value
```

- Chat Input과 Chat Output은 각각 정확히 하나만 둔다.
- Chat Input은 Agent에만 연결하고 각 Tool에는 별도 Message edge를 연결하지 않는다.
- Tool은 부모 `graph.session_id`를 자동 상속한다.
- 부모 Chat Input과 Chat Output만 메시지를 저장한다.
- 모든 V3 Tool은 `return_direct=false`로 둔다.
- Agent `max_iterations`는 5로 둔다. 최대 4회 Tool 호출 뒤 최종 답변을 생성하기 위한 값이며, 실제 Tool 호출 상한은 시스템 프롬프트의 최대 4회 규칙으로 제한한다.
- `cache_flow=true`는 child graph 구성만 캐시하며 조회 결과와 답변은 캐시하지 않는다.

## Tool 입력 계약

Agent에는 node ID 대신 두 필드만 노출한다.

```json
{
  "question": "이전 결과의 LOT을 대상으로 HOLD 이력을 조회해줘",
  "upstream_result_ref": "result:session-id:uuid"
}
```

- `question`은 필수다.
- `upstream_result_ref`는 선택이며 종속 실행에서만 사용한다.
- 첫 Tool이나 독립 Tool에는 ref를 전달하지 않는다.
- Tool은 현재 child graph에서 `upstream_result_ref` 입력 component를 동적으로 찾는다.
- 입력 component가 없거나 둘 이상이면 모호한 실행을 막기 위해 오류로 종료한다.

## 하위 Flow 요구사항

연계 입력을 받는 Flow에는 `upstream_result_ref`라는 입력 포트가 정확히 하나 있어야 한다. Data Analysis Flow에서는 분석 요청 로더 또는 전용 upstream loader에 이 입력을 둔다.

다음 Tool에서 재사용할 결과를 만드는 Flow는 terminal 출력으로 `api_response` Data를 제공하는 구성을 권장한다. Tool은 terminal `api_response`를 우선 선택하며, 없을 때만 terminal 출력이 정확히 하나인 Flow를 허용한다.

`api_response`에 다음 정보가 있어야 완전한 handoff가 가능하다.

```json
{
  "status": "ok",
  "message": "이상 LOT 12건을 확인했습니다.",
  "data": {
    "columns": ["LOT_ID", "OPER_NAME"],
    "rows": [{"LOT_ID": "LOT001", "OPER_NAME": "WB"}],
    "row_count": 12
  },
  "data_refs": [
    {
      "ref_id": "result:session-id:uuid",
      "role": "analysis_result",
      "row_count": 12,
      "columns": ["LOT_ID", "OPER_NAME"]
    }
  ]
}
```

Tool이 Agent에 반환하는 값은 `route_v3.tool_result.v1` 축약 계약이다. 전체 rows, trace, intent plan, SQL, pandas 코드는 제거된다. summary는 2,000자, entity ID preview는 컬럼당 50개, 전체 observation은 약 8KB로 제한한다.

## 이름·ID·권한

- `flow_id_selected`는 export에서 비워 둔다.
- 실행 시 현재 Langflow 사용자가 가진 정확한 Flow 이름으로 ID를 해석한다.
- 한 번 해석한 ID와 `updated_at`은 graph cache에 재사용한다.
- 오래되거나 다른 이름의 ID는 무시하고 이름으로 다시 해석한다.
- Route V3와 모든 child Flow는 같은 사용자로 import하고 같은 사용자 또는 API key로 실행한다.
- 실제 Flow 이름에 `(1)` 등이 붙으면 이름 해석이 실패할 수 있다.

## 메시지 중복 방지

Tool 실행 시 runtime tweak가 child Chat Input과 Chat Output의 `should_store_message`를 `false`로 설정한다. 따라서 Playground에는 부모 질문 한 건과 최종 종합 답변 한 건만 남아야 한다.

## 오류 정책

- ref 입력을 지원하지 않는 Tool에 ref를 전달하면 즉시 오류를 반환한다.
- `status=error`인 결과에 의존하는 다음 Tool은 호출하지 않는다.
- result store가 정상 ref를 만들지 못하면 `handoff_usable=false`가 된다.
- 전체 행을 자연어 질문에 복사하는 fallback은 사용하지 않는다.
- 관련 없는 Tool을 오류 복구 목적으로 호출하지 않는다.
