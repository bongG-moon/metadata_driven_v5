# Data Analysis Flow v5 Connection Guide

이 문서는 Langflow standalone 환경에서 v5 Data Analysis Flow를 조립하거나 import 결과를 확인하는 기준입니다. 가장 빠른 시작점은 `flow_exports/data_analysis_flow_v5_standalone.json`입니다. 기본 조회 모드는 `dummy`이며 live retriever 노드도 함께 들어 있습니다.

## 1. standalone 원칙

- `langflow_components/data_analysis_flow/*.py` 파일 하나가 Custom Component 노드 하나입니다. 파일 전체를 붙여 넣습니다.
- Prompt는 같은 폴더의 `*_prompt_template_ko.md`를 `Langflow Prompt Template`에 넣습니다.
- 모델 호출은 `Langflow Agent/LLM`을 사용합니다.
- custom component 사이의 repo-local import는 사용하지 않습니다.
- API key, DB password, token은 Flow JSON이나 코드에 저장하지 않고 Langflow Global Variable 또는 서버 환경변수로 제공합니다.
- 현재 Agent 노드는 Langflow Global Variable `GOOGLE_API_KEY`를 참조합니다. 회사 표준 Model Providers 설정을 쓸 경우 import 후 provider/key 연결을 그 설정에 맞게 교체합니다.

## 2. 요청, 상태, 메타데이터 후보

| From node.output | To node.input |
| --- | --- |
| `Chat Input.message` | `00 분석 요청 로더.question` |
| `Chat Input.message` | `00 MongoDB 세션 상태 로더.question` |
| `00 MongoDB 세션 상태 로더.loaded_state` | `00 분석 요청 로더.previous_state` |
| `00 분석 요청 로더.payload_out` | `01E 후속 질문 힌트 생성기.payload` |
| `01E 후속 질문 힌트 생성기.payload_out` | `01D 질문 기반 메타데이터 후보 생성기.payload` |
| `01A MongoDB 도메인 메타데이터 로더.domain_items` | `01D 질문 기반 메타데이터 후보 생성기.domain_items` |
| `01B MongoDB 테이블 카탈로그 로더.table_catalog_items` | `01D 질문 기반 메타데이터 후보 생성기.table_catalog_items` |
| `01C MongoDB 메인 변수 로더.main_flow_filters` | `01D 질문 기반 메타데이터 후보 생성기.main_flow_filters` |
| `01D 질문 기반 메타데이터 후보 생성기.metadata_candidates` | `02 의도 분석 변수 생성기.metadata_candidates_in` |
| `01E 후속 질문 힌트 생성기.payload_out` | `02 의도 분석 변수 생성기.payload` |

`01D` 기본값은 `max_domain_items=10`, `min_table_items=5`, `max_table_items=10`, `max_bytes=32768`입니다. 도메인은 관련 항목만 제한하고, 테이블은 관련 후보를 우선하면서 최소 5건을 유지하며, 메인 필터는 전체를 관련도 순으로 전달합니다. 후보에는 SQL/query template, endpoint, header, credential을 포함하지 않습니다. 32KB를 넘으면 도메인 후순위부터 줄이고 테이블 최소치와 메인 필터 전체를 우선 보존합니다.

## 3. Intent Prompt와 trusted catalog hydration

| From node.output | To node.input |
| --- | --- |
| `02.question` | `03 Intent Prompt.question` |
| `02.state_summary` | `03 Intent Prompt.state_summary` |
| `02.metadata_candidates` | `03 Intent Prompt.metadata_candidates` |
| `02.output_schema` | `03 Intent Prompt.output_schema` |
| optional `Text Input.text` | `03 Intent Prompt.specialized_prompt` |
| `03 Intent Prompt.prompt` | `Intent Agent/LLM.input` |
| `01E.payload_out` | `04 의도 계획 정규화기.payload` |
| `Intent Agent/LLM.response` | `04 의도 계획 정규화기.llm_response` |
| `04.payload_out` | `04A 신뢰 카탈로그 조회 작업 구성기.payload` |
| `01B.table_catalog_items` | `04A 신뢰 카탈로그 조회 작업 구성기.table_catalog_items` |

Intent LLM은 `dataset_key`, `source_alias`, `required_params`, `filters`만 선택합니다. `04A`가 LLM이 출력한 source 설정을 버리고 active table catalog의 설정을 주입합니다.

- `04A.retrieval_mode=dummy`: 모르는 dataset도 `source_type=dummy`, `dummy_only=true`로 유지해 더미 조회기로 전달합니다.
- `04A.retrieval_mode=live`: active catalog에 없는 dataset은 제거하고 error를 기록합니다.
- 조회 모드는 `04A.retrieval_mode` 한 곳에서만 설정합니다. `04A`가 선택값을 `request.retrieval_mode`에 기록하고 `07`은 그 값을 읽으므로 별도 모드 입력이 없습니다.

## 4. 이전 결과 복원, 검증, 조회

| From node.output | To node.input |
| --- | --- |
| `04A.payload_out` | `05 MongoDB 이전 결과 로더.payload` |
| `05.payload_out` | `06 조회 작업 검증기.payload` |
| `06.payload_out` | `07 데이터 조회 작업 라우터.payload` |
| `06.payload_out` | `13 소스 조회 결과 병합기.main_payload` |

`05`는 `data_ref`가 없으면 skip합니다. 이전 결과 재사용이 필요 없는 첫 질문도 같은 연결을 유지할 수 있습니다.

### Dummy branch

| From node.output | To node.input |
| --- | --- |
| `07.dummy_jobs` | `08 더미 데이터 조회기.payload` |
| `08.retrieval_payload` | `13.dummy_retrieval` |

### Live branches

| From node.output | To node.input |
| --- | --- |
| `07.oracle_jobs` | `09 Oracle 쿼리 조회기.payload` |
| `09.retrieval_payload` | `13.oracle_retrieval` |
| `07.h_api_jobs` | `10 H-API 데이터 조회기.payload` |
| `10.retrieval_payload` | `13.h_api_retrieval` |
| `07.datalake_jobs` | `11 데이터레이크 조회기.payload` |
| `11.retrieval_payload` | `13.datalake_retrieval` |
| `07.goodocs_jobs` | `12 Goodocs 조회기.payload` |
| `12.retrieval_payload` | `13.goodocs_retrieval` |

`07`은 전체 payload를 분기하지 않습니다. 각 retriever에는 `retrieval_job_bundle`, session/date 중심의 `request_context`, `routing_trace`만 전달합니다.

## 5. pandas 생성과 선택 helper

| From node.output | To node.input |
| --- | --- |
| `13.payload_out` | `14 조회 페이로드 어댑터.payload` |
| `14.payload_out` | `15 pandas 변수 생성기.payload` |
| `15.intent_plan_json` | `16 pandas Prompt.intent_plan_json` |
| `15.source_schema_json` | `16 pandas Prompt.source_schema_json` |
| `15.source_preview_json` | `16 pandas Prompt.source_preview_json` |
| `15.function_case_selection_json` | `16 pandas Prompt.function_case_selection_json` |
| `15.output_contract_json` | `16 pandas Prompt.output_contract_json` |
| `15.function_case_selection_json` | `15A 선택 helper 코드 생성기.function_case_selection_json` |
| `Text Input`의 전체 helper library | `15A 선택 helper 코드 생성기.helper_library` |
| `15A.selected_helper_code` | `16 pandas Prompt.function_case_helper_code` |
| `15A.selected_helper_code` | `17 pandas 실행/1회 복구기.function_case_helper_code` |
| `17B pandas 복구 프롬프트 템플릿.text` | `17 pandas 실행/1회 복구기.repair_prompt_template` |
| `16 pandas Prompt.prompt` | `Pandas Agent/LLM.input` |
| `14.payload_out` | `17 pandas 실행/1회 복구기.payload` |
| `Pandas Agent/LLM.response` | `17 pandas 실행/1회 복구기.llm_response` |

전체 helper library에는 `function_case_helper_code_input_example.py` 내용을 넣습니다. Prompt에는 `15A`가 실제 선택된 함수만 전달합니다. function case가 선택되지 않으면 빈 문자열입니다.

`17.function_case_helper_code`와 `17.repair_prompt_template`은 연결 가능한 일반 입력(`advanced=false`)으로 유지해야 합니다. Langflow 1.8.2는 advanced component input을 대상으로 하는 edge를 import/refresh 과정에서 제거합니다.

`17B`는 표준 Prompt Template이 아니라 raw 템플릿을 보관하는 visible Text Input입니다. `{failed_code}`, `{error_context_json}` 같은 값은 최초 실행이 실패한 뒤에만 만들어지므로, `17`이 오류 발생 시점에 이 raw 템플릿을 렌더링합니다. 따라서 Prompt를 canvas 밖에 숨기지 않으면서도 별도 pass/repair 실행 분기를 다시 만들지 않습니다.

## 6. 단일 pandas 실행 노드와 오류 시 1회 복구 경로

| From node.output | To node.input |
| --- | --- |
| `17 pandas 실행/1회 복구기.payload_out` | `23 MongoDB 결과 저장소.payload` |

별도 pass/repair canvas 분기와 두 번째 executor node는 사용하지 않습니다. 대신 `17` 내부에서 최초 pandas 실행이 성공하면 Repair LLM을 호출하지 않고, 실제 오류가 난 경우에만 Repair LLM을 정확히 한 번 호출한 뒤 수정 코드를 한 번 재실행합니다. `max_repair_attempts`는 UI와 코드 모두 최대 `1`로 제한됩니다.

최초 생성 Prompt와 Repair Prompt는 서로 다릅니다. 최초 Prompt는 intent/schema/preview/output contract를 바탕으로 새 코드를 설계하고, Repair Prompt는 여기에 실패 원본 코드, preamble이 적용된 실제 실행 코드, 오류·traceback·filter plan을 추가해 분석 의도는 유지하면서 오류 원인만 최소 수정합니다.

정확한 단독 구문 `import pandas as pd`, `import numpy as np`는 Repair까지 보내기 전에 executor가 제거하고 각각 `pd`, 제한형 `np` 계산 namespace를 주입합니다. 이는 `__import__`를 허용하는 방식이 아닙니다. 다른 import/import-from/혼합 import와 pandas/numpy 파일·네트워크 I/O API는 계속 `unsafe_code`로 차단되고 Repair 대상이 됩니다.

진단 출력을 켜면 `pandas 코드/실행` 섹션에서 허용 import 정규화 내역과 Repair의 시도 여부, LLM 호출 여부, 선택 결과, 최초/재시도/모델 호출 오류를 확인할 수 있습니다. 따라서 최종 오류가 초기 코드인지 재시도 코드인지 구분할 수 있습니다.

오류 시 Repair LLM에는 다음 정보가 전달됩니다.

- 첫 pandas LLM이 생성한 원본 코드 전체
- executor가 filter preamble을 붙여 실제로 실행한 코드
- 오류 유형·메시지·축약 traceback과 repairable error 목록
- intent plan과 pandas filter plan
- source schema와 source별 최대 5행 preview
- 의도 분석이 선택한 function case 정보와 해당 helper 코드만

복구 성공 시 최초 오류는 최종 active error에서 제거하고 `trace.inspection.pandas_repair`에 감사 정보로만 남깁니다. 복구 코드도 실패하면 마지막 재실행 오류를 최종 오류로 반환하면서 최초 오류와 코드 지문을 같은 repair trace에 보존합니다. 어느 경로든 `payload_out` 하나로 수렴하므로 최종 Chat Output도 하나입니다.

## 7. 결과 저장과 답변

| From node.output | To node.input |
| --- | --- |
| `17.payload_out` | `23 MongoDB 결과 저장소.payload` |
| `23.payload_out` | `18 답변 변수 생성기.payload` |
| `18.question` | `19 답변 Prompt.question` |
| `18.result_summary_json` | `19 답변 Prompt.result_summary_json` |
| `18.applied_scope_json` | `19 답변 Prompt.applied_scope_json` |
| `18.answer_context_json` | `19 답변 Prompt.answer_context_json` |
| `18.warnings_errors_json` | `19 답변 Prompt.warnings_errors_json` |
| optional 답변 지침 `Text Input.text` | `19 답변 Prompt.domain_answer_guidance` |
| `19 답변 Prompt.prompt` | `Answer Agent/LLM.input` |
| `23.payload_out` | `20 답변 응답 생성기.payload` |
| `Answer Agent/LLM.response` | `20.answer_text` |

Dummy source를 사용한 경우 `19`는 답변 본문에서 dummy 결과임을 반드시 밝힙니다.

## 8. 세션 저장을 포함한 최종 출력

| From node.output | To node.input |
| --- | --- |
| `20.payload_out` | `01 MongoDB 세션 상태 저장기.response_payload` |
| `01 세션 상태 저장기.payload_out` | `21 답변 메시지 어댑터.payload` |
| `01 세션 상태 저장기.payload_out` | `22 API 응답 생성기.payload` |
| `21.message` | `Chat Output.message` |
| `21.message` | `22 API 응답 생성기.display_message` |

세션 writer는 최종 출력과 병렬로 연결하지 않습니다. Message/API의 공통 선행 노드로 두어 저장 결과가 trace에 반영된 다음 응답을 만듭니다. 저장기는 전체 rows가 아니라 compact state와 `data_ref`를 저장합니다.

`21.show_analysis_evidence` 기본값은 OFF입니다. 분석 산출물/helper 결과가 필요한 디버깅에서만 켭니다. `include_diagnostics`, `show_intent_analysis`, `show_data_retrieval`, `show_pandas_code`도 운영 화면에서는 OFF를 권장합니다.

## 9. v5 환경 기본값

```dotenv
MONGODB_DATABASE=datagov
MONGODB_DOMAIN_COLLECTION=agent_v4_domain_items
MONGODB_TABLE_CATALOG_COLLECTION=agent_v4_table_catalog_items
MONGODB_MAIN_FLOW_FILTER_COLLECTION=agent_v4_main_flow_filters
MONGODB_RESULT_COLLECTION=agent_v4_result_store
MONGODB_SESSION_STATE_COLLECTION=agent_v4_session_states
```

v5는 위 v4 collection을 직접 사용하므로 별도 metadata 복사나 seed 이동이 필요하지 않습니다. dummy/live 전환은 환경변수가 아니라 `04A.retrieval_mode`에서 수행합니다.

## 10. 운영 전 체크리스트

- Flow import 후 모든 Custom Component가 build되는지 확인
- Intent/Pandas/Answer 모델 provider와 credential 확인
- `04A.retrieval_mode`가 의도한 `dummy` 또는 `live`인지 확인하고 `07`에 별도 모드 입력이 없는지 확인
- dummy 실행 시 API `data_mode=dummy`와 답변의 dummy 고지 확인
- live 실행 시 각 source별 1건 이상 조회 확인
- 정상 pandas 질문에서 `17` 내부 복구 LLM 호출이 0회인지 확인
- 실패 질문에서 `17` 내부 복구 LLM 호출이 최대 1회인지 확인
- API row가 `data.rows` 한 곳에만 있는지 확인
- 세션 writer trace가 최종 Message/API에 포함되는지 확인
- 동일 session으로 후속질문 2-turn 실행
