# Langflow Node Connection Guide Index

현재 운영 기본은 06 API Router입니다. Smart Router가 질문 유형을 분류하고 선택된 route의 `01 선택 Flow API 메시지 호출기`가 하위 Flow Run API를 호출합니다. 07은 Agent가 이름 기반 Cached Run Flow Tool 5개 중 하나를 선택하는 비교용 대안입니다.

먼저 읽을 문서:

| Guide | When to read |
| --- | --- |
| `langflow_components/route_flow/CONNECTION_GUIDE.md` | 06 API Router canvas를 확인할 때 |
| `langflow_components/route_flow_v2/CONNECTION_GUIDE.md` | 07 Agent + Tool Router canvas를 확인할 때 |
| `langflow_components/data_analysis_flow/CONNECTION_GUIDE.md` | 실제 데이터 조회/분석 flow를 만들 때 |
| `langflow_components/metadata_qa_flow/CONNECTION_GUIDE.md` | metadata/catalog/help 답변 flow를 만들 때 |
| `langflow_components/session_state_flow/CONNECTION_GUIDE.md` | 대화별 state load/write를 연결할 때 |

## Common Runtime Rule

06 API Router:

```text
Chat Input
-> Smart Router
-> 선택 route의 Flow API 메시지 호출기
-> route별 Chat Output
```

07 Agent + Tool Router:

```text
Chat Input
-> Agent <- 이름 기반 Cached Run Flow Tool 5개
-> 단일 Chat Output
```

subflow:

```text
Chat Input
-> 00 MongoDB Session State Loader
-> 00 Request Loader
-> subflow logic
-> Final API Response
-> 01 MongoDB Session State Writer

Final Message
-> Chat Output
```

## Common Component Rules

- 하위 flow의 00 request loader는 `Question`과 `Previous State`만 직접 연결합니다.
- session id는 별도 포트로 연결하지 않고 Chat/API message 또는 final API response에서 자동 추론합니다.
- 06 Router는 subflow payload를 조립하지 않고 원문 질문과 같은 session id를 하위 Run API에 전달합니다.
- Smart Router route output은 선택 route의 API 호출기 하나에만 연결합니다.
- 07 Router는 Agent가 정확히 하나의 Tool만 호출하고 `return_direct=true`로 하위 Flow 최종 응답을 그대로 반환합니다.
- Router의 Chat Input을 API 호출기나 Tool에 fan-out하지 않습니다. 부모 실행 세션은 컴포넌트가 자동 상속합니다.
- Web/API에서 router를 사용할 때는 선택된 subflow의 API/Data output을 우선 파싱합니다. subflow가 Message만 반환하면 message-only 응답으로 처리합니다.
- custom component는 standalone 파일로 동작해야 하며 sibling helper import를 사용하지 않습니다.
- input 이름과 output 이름이 같은 component 안에서 겹치지 않게 합니다.
