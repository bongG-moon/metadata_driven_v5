# Route Flow 연결 가이드 (06 API Router)

`route_flow`는 Smart Router가 선택한 route branch에서 하위 Langflow flow를 Run API로 호출하는 운영 기본 방식입니다.
Run Flow 노드는 호출 시간과 입력 전달 확인이 어려울 수 있으므로, 06은 API 호출 전용 구조로 정리합니다.

핵심 원칙은 단순합니다.

- API 호출 route의 Smart Router `Route Message`는 비웁니다.
- Smart Router route output이 사용자 원문을 그대로 내보내게 합니다.
- 선택된 branch의 메시지를 `01 선택 Flow API 메시지 호출기.flow_input`에 연결합니다.
- `Chat Input.message`는 Smart Router에만 한 번 연결합니다. Langflow가 실행 `session_id`를 각 API caller의 `session_id` 입력에 자동 주입하므로 별도 fan-out edge를 만들지 않습니다.
- `01`은 하위 flow API를 호출할 때 `input_value`와 `session_id`를 함께 보내고, 표시용 Message와 구조화 상태 Data를 함께 제공합니다.

## 1. 기본 구조

route마다 아래 노드 하나만 둡니다.

```text
Chat Input.message
  -> Smart Router.input

Smart Router.<route output>
  -> 01 선택 Flow API 메시지 호출기.flow_input

01 선택 Flow API 메시지 호출기.message
  -> Chat Output.input
```

`direct_answer`와 `clarification`은 하위 API를 호출하지 않고 Smart Router를 각 Chat Output에 바로 연결합니다.

```text
Smart Router.direct_answer / clarification
  -> Chat Output.input
```

Smart Router의 `process_case`는 선택되지 않은 output을 `stop()`으로 중단합니다. 중간 Gate를 terminal로 추가하면 Playground가 그 terminal들도 평가해 질문 JSON이 별도 카드로 표시될 수 있으므로 사용하지 않습니다. API route의 caller, API payload, session 전달과 호출 횟수는 변경하지 않습니다.

예를 들어 `data_analysis`, `metadata_qa`, `domain_saving` route를 사용한다면 각 route output 뒤에 `01 선택 Flow API 메시지 호출기`를 하나씩 복사해 둡니다.
각 `01` 노드의 `하위 Flow API URL`만 해당 flow URL로 다르게 입력합니다.

## 2. Smart Router 설정

Smart Router route table은 route 분류만 담당합니다.

| Route Name | Route Description | Route Message |
| --- | --- | --- |
| `data_analysis` | 생산량, 재공, 투입, 장비 ASSIGN 등 실제 제조 데이터 조회/분석 질문 | 비움 |
| `metadata_qa` | 등록된 데이터셋, 필수 파라미터, 쿼리, 도메인 용어, 계산 로직 확인 질문 | 비움 |
| `domain_saving` | 도메인 용어, 공정 그룹, 제품 그룹, 분석 규칙, 특화 함수 설명 저장 요청 | 비움 |
| `table_catalog_saving` | 데이터셋, source type, query template, required params, 컬럼 정보 저장 요청 | 비움 |
| `main_flow_filter_saving` | DATE, OPER_NAME, ORG 같은 공통 필터 정의 저장 요청 | 비움 |
| `direct_answer` | 인사, 기능 안내처럼 하위 flow API가 필요 없는 요청 | 사용자에게 보여줄 안내 메시지 |
| `clarification` | 요청이 모호해서 추가 설명이 필요한 경우 | 사용자에게 보여줄 확인 질문 |

중요: API 호출 route에 `{"route":"data_analysis"}` 같은 Route Message를 넣지 않습니다.
Smart Router는 Route Message가 있으면 원문 대신 그 메시지를 output으로 내보낼 수 있습니다.
그러면 하위 flow에는 사용자 질문이 아니라 route JSON이 들어가서 엉뚱한 답변이 나올 수 있습니다.

## 3. 01 노드 입력

`01 선택 Flow API 메시지 호출기`는 아래 입력을 사용합니다.

| 입력 | 연결/입력 방식 |
| --- | --- |
| `Flow 입력` | Smart Router의 해당 route output을 연결합니다. Route Message가 비어 있으면 사용자 원문이 들어옵니다. |
| `하위 Flow endpoint/URL` | `endpoint_name`, `/api/v1/run/<endpoint_name>` 또는 전체 URL을 사용할 수 있습니다. 상대값은 `LANGFLOW_BASE_URL` 기준으로 해석됩니다. |
| `Langflow API 키` | 필요한 환경에서만 입력합니다. 비어 있으면 `LANGFLOW_API_KEY` 환경변수를 사용합니다. |
| `세션 ID` | advanced 입력입니다. Router 실행 시 Langflow가 부모 실행 session_id를 자동 주입합니다. 외부에서 명시한 session_id도 같은 값으로 전달됩니다. |
| `Route 이름` | 상태 Data에 기록할 branch 이름입니다. |
| `연결 제한 시간(초)` | 기본값 5초입니다. |
| `응답 제한 시간(초)` | 기본값 240초입니다. 외부 Web/API client는 Router 판단과 응답 직렬화 여유를 포함해 300초 이상으로 둡니다. |

`import_ready_flows` 묶음은 모든 API caller에 하위 Flow의 고정 `endpoint_name` 경로가 이미 입력되어 있으므로 Flow ID 치환이나 canvas 재연결이 필요 없습니다. 기본 주소가 `http://127.0.0.1:7860`이 아니면 `LANGFLOW_BASE_URL`만 설정합니다.

## 4. 하위 Flow API 호출 형식

`01`은 Langflow Run API에 아래 payload만 보냅니다.

```json
{
  "input_value": "사용자 원문",
  "input_type": "chat",
  "output_type": "chat",
  "session_id": "route_flow_..."
}
```

하위 flow가 API용 구조화 응답을 만들어야 한다면 router가 아니라 하위 flow 내부에서 Message를 그렇게 만들도록 수정합니다.
06 API Router는 표시 Message를 재구성하지 않습니다. `status_data`는 운영 확인용 구조화 상태이며 하위 응답 payload를 복제하지 않습니다.

HTTP 호출은 프로세스 내 `requests.Session`을 재사용합니다. 저장 route에는 자동 retry를 적용하지 않습니다.

웹 구현과의 가장 큰 차이는 `session_id`입니다.
웹은 Langflow API를 호출할 때 항상 현재 대화의 `session_id`를 전달합니다.
06 API Router에서 `session_id`를 보내지 않으면 Langflow가 하위 flow ID 기반 기본 세션을 사용할 수 있고, 이 경우 이전 실행 맥락이 섞여 사용자 질문과 다른 답변처럼 보일 수 있습니다.

## 5. Route별 URL 설정

route별 URL 예시는 `langflow_components/route_flow/ROUTE_API_NODE_SETTINGS_EXAMPLE.md`를 참고합니다.

## 6. 오류 점검

`01`은 `Flow 입력`이 아래처럼 route JSON만 들어온 경우 API 호출을 막고 안내 메시지를 반환합니다.

```json
{"route":"data_analysis"}
```

이 메시지가 보이면 Smart Router의 API 호출 route에서 Route Message를 비우면 됩니다.

## 7. 검증 기준

- API 호출 route의 Route Message가 비어 있다.
- `Chat Input.message`의 outgoing edge는 Smart Router 한 개뿐이고 API caller `session_source`로 가는 edge는 없다.
- `direct_answer`, `clarification`은 Smart Router에서 각 Chat Output으로 직접 연결되고 중간 Gate가 없다.
- Smart Router route output이 사용자 질문 원문이다.
- `01`의 실제 API payload에서 `input_value`가 사용자 질문과 동일하고 `session_id`가 함께 전달된다.
- `01.message`를 Chat Output에 연결하면 하위 flow가 반환한 실제 Message가 그대로 보인다.
- `01.status_data`에서 route, HTTP status, downstream status, duration, error를 확인할 수 있다.
- Router에는 route-flow 매핑 상수나 하위 payload 복제 envelope가 없다.
