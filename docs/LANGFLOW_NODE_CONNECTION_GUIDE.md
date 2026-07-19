# Langflow Node Connection Guide Index

현재 운영 기본은 06 API Router입니다. Smart Router가 질문 유형을 분류하고 선택된 route의 `01 선택 Flow API 메시지 호출기`가 하위 Flow Run API를 호출합니다. 07은 단일 하위 Flow를 고르는 비교용 대안, 08은 저장된 Workflow Skill을 검증된 순서로 실행하는 Orchestrator, 09는 08이 사용할 Workflow Skill 저장 Flow입니다.

먼저 읽을 문서:

| Guide | When to read |
| --- | --- |
| `langflow_components/route_flow/CONNECTION_GUIDE.md` | 06 API Router canvas를 확인할 때 |
| `langflow_components/route_flow_v2/CONNECTION_GUIDE.md` | 07 Agent + Tool Router canvas를 확인할 때 |
| `langflow_components/route_flow_v4/CONNECTION_GUIDE.md` | 08 문서 기반 최대 4단계 순차 Workflow canvas를 확인할 때 |
| `langflow_components/workflow_skill_saving_flow/CONNECTION_GUIDE.md` | 09 Workflow Skill 등록·검수·MongoDB 저장 canvas를 확인할 때 |
| `langflow_components/workflow_skill_saving_flow/INPUT_EXAMPLES.md` | 09 Flow에 넣을 자연어 등록 예시와 replace 사례가 필요할 때 |
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

08 Workflow Orchestrator:

```text
Chat Input
-> 00A MongoDB Workflow Registry 후보 로더
-> 기본 Language Model 계획기
-> 결정론적 계획 파서
-> 기본 Loop <- 이름 기반 Cached Run Flow Tool 6개
-> 정확한 Tool 한 개씩 순차 실행
-> 기본 Language Model 최종 합성
-> 단일 Chat Output / API 응답
```

09 Workflow Skill 저장 Flow:

```text
Chat Input
-> 요청 로더 -> Prompt Template -> 기본 Language Model
-> 결과 정규화기 -> 기존 항목/유사 항목 조회기
-> 결정론적 Writer(dry-run 또는 MongoDB 저장)
-> 단일 Chat Output / API 응답
```

10 HTML Visualization Flow:

```text
Chat Input
-> HTML 시각화 생성기 <- Data Analysis result_ref / MongoDB
-> 단일 Chat Output
   + terminal api_response
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
- 08 Router는 `datagov.agent_v4_workflow_skills`에서 질문 관련 후보만 최대 8개·64KB로 읽고, 파서가 저장된 canonical 단계와 Tool 이름을 검증한 뒤 기본 Loop로 최대 4개 Tool을 순차 실행합니다. 종속 호출에만 직전 `result_ref`를 `upstream_result_ref`로 전달합니다.
- 09 저장 Flow는 `dry_run=true`가 기본이며, 실제 저장 전 최대 4단계·dependency·`result_ref`·32KB payload를 Python에서 다시 검증합니다.
- Router의 Chat Input을 API 호출기나 Tool에 fan-out하지 않습니다. 부모 실행 세션은 컴포넌트가 자동 상속합니다.
- Web/API에서 router를 사용할 때는 선택된 subflow의 API/Data output을 우선 파싱합니다. subflow가 Message만 반환하면 message-only 응답으로 처리합니다.
- custom component는 standalone 파일로 동작해야 하며 sibling helper import를 사용하지 않습니다.
- input 이름과 output 이름이 같은 component 안에서 겹치지 않게 합니다.

## 구조화 최종 출력은 Python에서 선언

구조화 `Data`를 Run Flow/API의 최종 결과로 내보내는 custom component는 Flow JSON을 직접 수정하지 않습니다. 최종 응답 component의 Python 클래스에 다음 선언을 유지합니다.

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.is_output = True
```

Langflow에서 이 코드를 저장하거나 프로젝트 생성기로 Flow를 다시 만들면 graph output 설정이 자동 생성됩니다. 따라서 다른 개발자가 알아야 할 작업은 다음 두 가지뿐입니다.

1. 최종 구조화 응답 component에 위 선언을 유지합니다.
2. 해당 `api_response` 포트는 다른 노드에 연결하지 않고 terminal로 둡니다. 사용자 화면용 Message는 별도 Message Adapter에서 하나의 Chat Output으로 연결합니다.

`Output(...)`은 포트와 자료형을 정의할 뿐 graph output 여부를 정하지 않습니다. 내려받은 JSON의 `is_output` 값을 사람이 직접 추가하거나 수정하지 않습니다.
