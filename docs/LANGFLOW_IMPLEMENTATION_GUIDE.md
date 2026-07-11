# Langflow Implementation Guide

이 프로젝트는 metadata만 바꾸면 다른 업무에도 재사용할 수 있는 Langflow 기반 metadata-driven agent를 목표로 합니다. 특정 질문이나 특정 제조 조건을 Python 코드에 하드코딩하지 않고, domain/table/filter metadata와 LLM planning을 통해 동작하도록 구성합니다.

## Runtime Shape

현재 권장 runtime은 split flow 구조입니다.

```text
main router canvas
Chat Input
-> Smart Router
-> route별로 URL이 고정된 Langflow Run API caller 하나
-> 선택 route의 subflow 응답
-> Chat Output
```

각 하위 flow는 독립 실행 가능한 subflow입니다.

```text
subflow
Chat Input
-> 00 MongoDB Session State Loader
-> 00 Request Loader
-> subflow logic
-> Final API Response
-> 01 MongoDB Session State Writer

Final Message
-> Chat Output
```

main router는 어떤 route로 보낼지 판단합니다. 현재 v5 export는 실행 속도와 endpoint 제어를 위해 route별 API caller에 대상 subflow의 `/api/v1/run/{endpoint_name}`을 미리 설정합니다. state load/write, metadata QA 판단, data analysis 실행 준비는 각 subflow 안에서 처리합니다.

같은 bundle에는 비교용 `Agent + Tool Mode Router`도 포함됩니다.

```text
Chat Input
-> Agent
   <- 이름 기반 Cached Flow Tool 5종
-> Chat Output 1개
```

이 Router는 import 후 바뀐 Flow ID를 export에 고정하지 않고 이름으로 실제 ID를 해석합니다. `cache_flow=true`는 하위 Flow 그래프만 캐시하며 데이터 조회와 답변 결과는 캐시하지 않습니다. 각 Tool은 대상 Flow의 `ChatInput.input_value`만 Agent 인자로 노출하고 `return_direct=true`로 Tool 뒤 추가 답변 재작성을 생략합니다.

## Main Router Flow

| Node | Role |
| --- | --- |
| Chat Input | 사용자 입력을 Smart Router로 전달 |
| Smart Router | route table과 Additional Instructions 기준으로 route 판단 |
| route별 API caller | 각 branch에서 미리 설정된 subflow Run API 실행 |
| route별 Chat/API Output | 선택된 subflow의 최종 응답 반환 |

Smart Router route output은 route별 API caller에 직접 연결합니다. 각 caller에는 bundle이 정한 고정 `endpoint_name`이 들어가며, `LANGFLOW_BASE_URL`과 필요 시 `LANGFLOW_API_KEY`를 사용합니다. Router는 API 방식으로 하위 Flow를 호출하므로 canvas edge나 Flow ID를 수동으로 다시 연결하지 않습니다.

`direct_answer`와 `clarification`은 Smart Router에서 각 Chat Output으로 직접 연결합니다. 별도 terminal Gate는 Playground에 질문 JSON 카드를 추가할 수 있으므로 두지 않습니다. 운영 기본은 이 API Router이며, 자연어 Tool 선택 방식을 비교하려면 `07_agent_tool_router_flow_v5_standalone.json`을 사용합니다.

## Data Analysis Flow

`data_analysis_flow`는 실제 데이터 조회, pandas 분석, result store 저장, 답변 생성을 담당합니다.

```text
00 MongoDB Session State Loader
-> 00 Analysis Request Loader
-> 01E Follow-up Hint Builder
-> 01A/01B/01C MongoDB Metadata Loaders
-> 01D Metadata Candidate Builder
-> 02 Intent Variables / 03 Intent Prompt
-> Intent LLM
-> 04 Intent Plan Normalizer
-> 04A Trusted Catalog Retrieval Job Builder
-> 05 Previous Result Loader
-> 06 Retrieval Job Validator
-> 07 Retrieval Job Router
-> 08 dummy or 09~12 live source retrievers
-> 13 Source Retrieval Merger
-> 14 Retrieval Payload Adapter
-> 15 Pandas Variables / 15A Selected Helper
-> 16 Pandas Prompt
-> Pandas Code LLM
-> 17 Pandas Executor with error-only one-shot repair
-> 23 MongoDB Result Store
-> 18 Answer Variables / 19 Answer Prompt
-> Answer LLM
-> 20 Answer Response Builder
-> 01 MongoDB Session State Writer
-> 21 Message Adapter / 22 API Response Builder
-> Chat Output
```

API/session state 저장 경로:

```text
20 Answer Response Builder.Payload
  -> 01 MongoDB Session State Writer.Response Payload

01 MongoDB Session State Writer.Payload
  -> 21 Message Adapter / 22 API Response Builder
```

## Source Retrieval

검증 단계에서는 dummy retriever만 연결해도 됩니다.

```text
07 Retrieval Job Router.Dummy Jobs
  -> 08 Dummy Data Retriever.Payload

08 Dummy Data Retriever.Retrieval Payload
  -> 13 Source Retrieval Merger.Dummy Retrieval
```

운영에서는 source type별 retriever를 병렬로 연결합니다. 각 retriever는 자기 source type에 맞는 retrieval job이 없으면 `skipped=true`를 반환하고, merger는 skipped payload를 무시합니다.

## LLM Placement

| Purpose | Connection |
| --- | --- |
| Route classification | `Chat Input -> Smart Router` |
| Intent planning | `03 Intent Prompt -> Intent LLM -> 04 Intent Plan Normalizer -> 04A Trusted Catalog Job Builder` |
| Pandas code generation | `16 Pandas Prompt -> Pandas Code LLM -> 17 Pandas Executor` |
| Pandas repair | `17` 내부에서 실제 실행 오류일 때만 이전 코드·오류 문맥으로 Repair LLM 1회 호출 후 1회 재실행 |
| Final answer writing | `19 Answer Prompt -> Answer LLM -> 20 Answer Response Builder` |

LLM 출력은 그대로 신뢰하지 않습니다. route, intent JSON, pandas code는 normalizer/executor에서 metadata와 safety rule을 통과해야 합니다.

## Hardcoding Policy

- DA/WB/HBM 같은 업무 용어는 code에 직접 박지 않고 metadata alias/condition을 통해 해석합니다.
- 특정 질문 하나를 고치기 위해 executor에 후처리 예외를 넣지 않습니다.
- 제품 grain, rank group, filter column, output column은 질문 의도와 metadata를 기준으로 LLM plan/pandas prompt에서 결정하게 합니다.
- fallback은 flow가 멈추지 않기 위한 최소 보정만 수행합니다. 업무별 분석 로직을 새로 만들어내는 위치가 아닙니다.

## Payload Contract

중간 payload는 필요한 compact 정보만 담습니다.

| Field | Meaning |
| --- | --- |
| `request` | session id, question, reference date |
| `state` | chat history, context, current_data |
| `metadata` | domain, table catalog, main flow filters |
| `metadata_route` | router가 정규화한 route 결정 |
| `intent_plan` | normalized intent, analysis kind, step plan |
| `retrieval_jobs` | dataset별 조회 요청 |
| `runtime_sources` | 현재 turn pandas 실행에 쓰는 source rows |
| `runtime_source_refs` | compact state에서 복원 가능한 source refs |
| `source_results` | compact retrieval trace |
| `analysis` | pandas 실행 결과 |
| `data` | 최종 사용자 표시 데이터 |
| `applied_scope` | 적용 dataset/filter/params/metadata refs |
| `answer_message` | 최종 답변 |

## Standalone Component Rules

- 각 numbered custom component는 하나의 파일만 Langflow에 붙여도 동작해야 합니다.
- sibling/project helper import를 사용하지 않습니다.
- input 이름과 output 이름이 같은 component 안에서 겹치지 않게 합니다.
- process-specific rule은 Python code보다 metadata 또는 prompt contract로 둡니다.

## Validation

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python -m compileall langflow_components tests tools
python -m pytest -q
```

Langflow Desktop component parser 검증:

```powershell
$py='C:\Users\qkekt\AppData\Local\com.LangflowDesktop\.langflow-venv\Scripts\python.exe'
$script=@'
from pathlib import Path
from lfx.custom.eval import eval_custom_component_code
root = Path(r'C:\Users\qkekt\Desktop\metadata_driven_v5\langflow_components')
for path in sorted(root.rglob('*.py')):
    code = path.read_text(encoding='utf-8')
    cls = eval_custom_component_code(code)
    instance = cls(_code=code)
print('init_ok')
'@
$script | & $py -
```
