# Workflow Skill 저장 Flow 연결 가이드

## 노드 연결

| 순서 | Source 출력 | Target 입력 |
| --- | --- | --- |
| 1 | Chat Input `message` | 요청 로더 `raw_text` |
| 2 | 요청 로더 `payload_out` | 변수 생성기 `payload` |
| 3 | 변수 생성기 `source_text` | Prompt Template `source_text` |
| 4 | Prompt Template `prompt` | 기본 Language Model `input_value` |
| 5 | 요청 로더 `payload_out` | 결과 정규화기 `payload` |
| 6 | 기본 Language Model `text_output` | 결과 정규화기 `llm_response` |
| 7 | 결과 정규화기 `payload_out` | 유사 항목 조회기 `payload` |
| 8 | 기존 항목 로더 `existing_items` | 유사 항목 조회기 `existing_items` |
| 9 | 유사 항목 조회기 `payload_out` | 검수/저장 처리기 `payload` |
| 10 | 검수/저장 처리기 `payload_out` | 응답 정규화기 `payload` |
| 11 | 응답 정규화기 `payload_out` | 메시지 어댑터 `payload` |
| 12 | 응답 정규화기 `payload_out` | API 응답 생성기 `payload` |
| 13 | 메시지 어댑터 `message` | API 응답 생성기 `display_message` |
| 14 | 메시지 어댑터 `message` | 단일 Chat Output `input_value` |

중간 LLM 출력, 기존 항목, JSON 응답을 Chat Output에 직접 연결하지 않습니다.

## Standalone MongoDB 설정

다음 세 노드에서 같은 값을 사용합니다.

- `00 Workflow Skill 기존 항목 로더`
- `05 Workflow Skill 유사 항목 조회기`
- `07 Workflow Skill 검수/저장 처리기`

| 입력 | 값 |
| --- | --- |
| MongoDB 연결 URI | Langflow global variable `MONGO_URL` 또는 직접 입력 |
| MongoDB 데이터베이스 | `datagov` |
| 컬렉션 이름 | `agent_v4_workflow_skills` |

Standalone 환경에서는 OS 환경변수에만 의존하지 않습니다. Import된 JSON에서 `mongo_uri` 입력이 보이고 `MONGO_URL` global variable에 연결되어 있는지 확인합니다.

## 실행 순서

1. 기본 Language Model 노드에 실제 환경에서 사용할 모델을 연결합니다.
2. `dry_run=true`, 원하는 `duplicate_action`으로 입력 예시를 실행합니다.
3. 등록 대상 표와 실행 순서에서 Tool·질문·dependency·handoff를 확인합니다.
4. 실제 저장이 필요하면 `dry_run=false`로 바꾸고 같은 입력을 다시 실행합니다.
5. Route V4를 새로 실행해 intent example 또는 정확한 workflow key로 호출합니다.

## 오류 해석

- `identity_lookup_unavailable`: URI 또는 기존 문서 조회에 실패해 중복 여부를 판단하지 못했습니다.
- `ambiguous_replace_target`: 유사한 기존 Skill이 여러 건입니다. key·alias를 정리한 뒤 다시 실행합니다.
- `dependency_not_prior`: 뒤 단계나 존재하지 않는 step을 참조했습니다.
- `invalid_result_ref_contract`: 데이터 분석이 아닌 Tool에 실제 결과 ref를 전달하려 했습니다.
- `too_many_steps`: 한 Workflow에 5개 이상 단계가 있습니다. 업무를 나누거나 4개 이하로 줄입니다.

Human-in-the-loop `ask` 분기는 없습니다. 필요한 정보가 부족하면 저장하지 않고 보완 항목을 최종 응답에 표시합니다.
