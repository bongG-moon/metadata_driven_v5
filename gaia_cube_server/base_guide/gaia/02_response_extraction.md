# GAIA 응답에서 최종 답변 추출

## 결론

CUBE로 전달할 값은 GAIA 응답 전체가 아니라 **가장 마지막의 유효한 Chat Output에 포함된 최종 답변 문자열**이다.

2026-08-24에 확인한 실제 응답에서는 아래 경로가 정규화된 최종 답변이다.

```python
data = response.json()
result = data["outputs"][0]["outputs"][0]["results"]

answer = result["gaia_response"]["data"]["answer"]
metadata = result["gaia_response"]["data"].get("metadata", {})
session_id = data.get("session_id")
graph_run_id = (
    result["message"]["data"]
    .get("session_metadata", {})
    .get("graph_run_id")
)
```

운영 서버는 Flow의 출력 순서가 바뀌어도 오래된 답변을 보내지 않도록 마지막 `Chat Output`을 먼저 찾은 뒤, 그 안의 `results.gaia_response.data.answer`를 우선 사용한다. `metadata`, `graph_run_id`, `session_id`는 CUBE 메시지 본문에 넣지 않는다.

단순히 루트 배열의 마지막 원소만 고정 경로로 읽으면 Flow 구성 변화에 취약하다. 다음 순서로 처리한다.

1. 루트 `outputs`를 뒤에서부터 순회한다.
2. 각 항목의 내부 `outputs`도 뒤에서부터 순회한다.
3. `component_display_name == "Chat Output"` 또는 `component_id`가 `ChatOutput-`으로 시작하는 가장 마지막 출력을 선택한다.
4. 선택한 출력에서 아래 우선순위로 비어 있지 않은 답변 문자열을 추출한다.
5. 최종 출력이 오류 상태이거나 문자열을 찾지 못하면 이전 답변을 대신 보내지 않고 실행 실패로 처리한다.

## 답변 필드 우선순위

제공받은 응답에는 같은 답변이 여러 위치에 중복되어 있다. GAIA 전용 응답을 우선하고 표준 Langflow 메시지 필드를 fallback으로 사용한다.

| 순위 | 선택한 Chat Output 기준 경로 | 용도 |
| --- | --- | --- |
| 1 | `results.gaia_response.data.answer` | GAIA 전용 최종 답변 |
| 2 | `results.message.data.gaia_response.answer` | Message 내부 GAIA 답변 |
| 3 | `results.message.data.text` | 표준 Langflow Message 텍스트 |
| 4 | `outputs.gaia_response.message.answer` | 직렬화된 GAIA 출력 |
| 5 | `outputs.message.message` | 직렬화된 텍스트 출력 |
| 6 | `artifacts.message` | Chat Output artifact 텍스트 |
| 7 | `messages`의 마지막 Machine 메시지의 `message` | 최종 호환 fallback |

향후 GAIA 공식 응답 스키마가 하나의 canonical 필드를 명시하면 이 우선순위를 그 계약에 맞게 축소한다.

## 기준 구현 예시

```python
from collections.abc import Mapping
from typing import Any


class GaiaResponseError(RuntimeError):
    pass


def _get(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    component_outputs: list[Mapping[str, Any]] = []
    outer_outputs = payload.get("outputs")
    if isinstance(outer_outputs, list):
        for outer in reversed(outer_outputs):
            if not isinstance(outer, Mapping):
                continue
            inner_outputs = outer.get("outputs")
            if not isinstance(inner_outputs, list):
                continue
            component_outputs.extend(
                item for item in reversed(inner_outputs) if isinstance(item, Mapping)
            )

    if not component_outputs:
        raise GaiaResponseError("GAIA response has no component outputs")

    selected = next(
        (
            item
            for item in component_outputs
            if item.get("component_display_name") == "Chat Output"
            or str(item.get("component_id") or "").startswith("ChatOutput-")
        ),
        component_outputs[0],
    )

    if _get(selected, "results", "message", "data", "error") is True:
        raise GaiaResponseError("final GAIA Chat Output is an error")

    candidate_paths = (
        ("results", "gaia_response", "data", "answer"),
        ("results", "message", "data", "gaia_response", "answer"),
        ("results", "message", "data", "text"),
        ("outputs", "gaia_response", "message", "answer"),
        ("outputs", "message", "message"),
        ("artifacts", "message"),
    )
    for path in candidate_paths:
        answer = _nonempty_text(_get(selected, *path))
        if answer is not None:
            return answer

    messages = selected.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, Mapping):
                continue
            sender = str(message.get("sender") or "").strip().lower()
            answer = _nonempty_text(message.get("message"))
            if answer is not None and sender in {"machine", "ai", "assistant"}:
                return answer

    raise GaiaResponseError("final GAIA Chat Output has no answer text")
```

## CUBE 전달값과 내부 보존값

CUBE 메시지 본문에는 추출한 `answer`만 사용한다. 다음 값은 응답 전달, 추적 및 장애 분석을 위한 내부 실행 결과로 별도 보존할 수 있다.

- 루트 및 Message의 `session_id`
- `flow_id`
- `run_id`
- `session_metadata.graph_run_id`
- `gaia_response.metadata.trace_id`
- `docs`, `images`, `knowhows`, `followup_questions`, `urls` 등의 metadata
- GAIA HTTP status와 처리 시간

동일한 답변이 들어 있는 중복 필드를 모두 저장하거나 CUBE로 그대로 노출할 필요는 없다. 원본 응답 전체의 영구 저장 여부는 운영 보안·개인정보 정책을 확인한 뒤 결정한다.

## 실패 처리

- HTTP 오류, JSON 파싱 실패, 빈 `outputs`, 최종 Chat Output 오류 및 답변 누락을 서로 구분한다.
- 최종 출력 추출에 실패했다고 이전 Chat Output의 오래된 답변을 보내지 않는다.
- 로그에는 인증 키와 전체 사용자 메시지를 남기지 않는다.
- CUBE 발송 실패 시 GAIA 실행을 무조건 다시 호출하지 않도록, 추출된 답변과 실행 식별자를 발송 상태와 분리해 관리한다.
