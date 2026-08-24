# GAIA 응답에서 최종 답변 추출

## CUBE로 보낼 값

GAIA 응답 전체 JSON이 아니라, 가장 마지막 `Chat Output`의 최종 답변 문자열만 CUBE로 보낸다.

사용자가 제공한 실제 응답에서 가장 우선할 값은 아래 경로다.

```python
answer = result["gaia_response"]["data"]["answer"]
```

여기서 `result`는 최종 Chat Output의 `results`다.

## 서버가 찾는 순서

GAIA Flow의 출력 순서가 바뀌어도 오래된 답변을 보내지 않도록 서버는 다음 순서로 찾는다.

```text
outputs의 마지막 항목부터 확인
  -> 그 안의 outputs를 마지막 항목부터 확인
  -> component_display_name이 "Chat Output"인 항목 선택
  -> results.gaia_response.data.answer 사용
  -> 없으면 results.message.data.text 사용
```

첫 번째 값이 GAIA가 정규화해 준 답변이므로 우선한다. 두 번째 값은 호환용 fallback이다.

## 사용 예시

```python
data = response.json()
result = data["outputs"][0]["outputs"][0]["results"]

# CUBE 사용자에게 보낼 문장
answer = result["gaia_response"]["data"].get("answer", "")

# 다음 대화를 이어갈 때 서버가 다시 쓸 값
session_id = data.get("session_id")

# 필요할 때만 장애 추적용으로 확인할 값
graph_run_id = (
    result["message"]["data"]
    .get("session_metadata", {})
    .get("graph_run_id")
)
```

`metadata`, `graph_run_id`, `files` 등은 현재 CUBE 일반 텍스트 답변에 넣지 않는다.

## 실패 처리

마지막 Chat Output이 오류이거나 답변 문자열이 비어 있으면, 서버는 이전 질문의 답변을 대신 보내지 않는다. 대신 설정된 일반 오류 문구를 CUBE에 보낸다.
