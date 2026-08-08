# Agent Tool Router Continuation

기존 `06. v5_agent_tool_router`와 독립된 추가 Flow용 standalone 컴포넌트입니다.

- Agent 공개 schema는 `question` 하나입니다.
- Agent는 `max_iterations=1`, Tool은 `return_direct=true`를 유지합니다.
- Data Analysis child의 `api_response.continuation.status=pending`만 자동 후속 실행 신호로 사용합니다.
- 답변 문자열을 파싱하지 않습니다.
- `upstream_result_ref`, `continuation_ref`, `continuation_contract`, `skip_intermediate_answer` 포트가 모두 있을 때만 2차 실행합니다.
- 전체 rows, trace, pandas code는 Agent observation에 전달하지 않습니다.
- 최대 2단계, 동일 세션, 전체 timeout, 중복 단계 차단을 적용합니다.
