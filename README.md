# metadata_driven_v5

Langflow standalone 환경에서 실행하는 메타데이터 기반 제조 데이터 분석 에이전트입니다. v4 구현과 제공된 `1.1 data_analysis_flow_후속질문.json`을 기준으로 다시 구성했으며, 현재 기본 실행 모드는 `dummy`입니다. Oracle, H-API, Datalake, Goodocs 컴포넌트도 export에 포함되어 있어 환경 설정 후 `live`로 전환할 수 있습니다.

## 바로 확인할 파일

- 가져오기용 Flow: `flow_exports/data_analysis_flow_v5_standalone.json`
- 전체 7개 Flow 단일 Import: `import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`
- 운영 기본 Router: `import_ready_flows/06_api_router_flow_v5_standalone.json`
- Agent + Tool Mode Router: `import_ready_flows/07_agent_tool_router_flow_v5_standalone.json`
- 06 커스텀 원본: `langflow_components/route_flow/`
- 07 커스텀 원본: `langflow_components/route_flow_v2/`
- 전체 커스텀 원본 감사: `docs/CUSTOM_COMPONENT_SOURCE_AUDIT_20260712.md`
- 현재 버전 정리 보고서: `docs/CURRENT_VERSION_CLEANUP_20260712.md`
- Flow 재생성 도구: `tools/build_v5_data_analysis_flow.py`
- Data Analysis 연결표: `langflow_components/data_analysis_flow/CONNECTION_GUIDE.md`
- v5 변경 보고서: `docs/V5_IMPLEMENTATION_REPORT.md`
- Router 수정·Agent Tool 구현 보고서: `docs/ROUTER_FIX_AND_AGENT_TOOL_IMPLEMENTATION_REPORT_20260711.md`
- payload 계약: `docs/V5_PAYLOAD_CONTRACT.md`
- 환경변수 예시: `.env.example`

## 실행과 검증

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python -m pip install -e .
python -m pytest -q --basetemp=.pytest_tmp
python tools\build_v5_data_analysis_flow.py
python tools\validate_representative_questions.py
python tools\validate_flow_component_sources.py
```

Flow JSON은 제공된 v4 export와 현재 폴더의 custom component/prompt 원본을 결합해 재현 가능하게 생성합니다. 빌더를 다시 실행한 뒤 테스트가 통과하면 checked-in export와 코드가 일치합니다.

Langflow가 설치된 가상환경에서는 실제 LFX parser와 실행 서버도 검증할 수 있습니다.

```powershell
python tools\validate_langflow_runtime.py
python tools\validate_langflow_runtime.py --server-url http://127.0.0.1:7860 --partial-build --stop-component-id "Prompt Template-AUpQz"
```

별도 terminal 환경이 필요하면 export의 기준 버전에 맞춰 설치합니다.

```powershell
uv venv .langflow-venv --python 3.12
uv pip install --python .langflow-venv\Scripts\python.exe "langflow==1.8.2"
.langflow-venv\Scripts\langflow.exe run --host 127.0.0.1 --port 7860 --no-open-browser
```

## standalone 배치 원칙

- 각 `langflow_components/**/*.py`는 Langflow Custom Component 한 노드에 파일 전체를 붙여 넣을 수 있도록 작성했습니다.
- Prompt와 모델 호출은 custom component에 숨기지 않고 Langflow 기본 Prompt Template 및 Agent/LLM 노드에 둡니다.
- 저장소 내부 모듈을 import하지 않아도 각 custom component가 동작하도록 구성합니다.
- 운영 비밀값은 코드나 Flow JSON에 넣지 않고 Langflow 전역변수 또는 프로세스 환경변수로 제공합니다.
- v5는 별도 데이터 복제 없이 v4와 같은 `datagov` 데이터베이스의 `agent_v4_domain_items`, `agent_v4_table_catalog_items`, `agent_v4_main_flow_filters`, `agent_v4_result_store`, `agent_v4_session_states` collection을 직접 재사용합니다.
- Flow 이름과 API endpoint는 v5로 유지되며, MongoDB collection만 v4와 공유합니다. 환경변수로 다른 full collection name을 지정한 경우에는 그 값이 우선합니다.

## v5에서 달라진 핵심

1. 전체 메타데이터를 LLM에 넘기지 않습니다. 도메인은 관련 항목만 최대 10건, 테이블은 관련 후보 최소 5건·최대 10건, 메인 필터는 전체를 전달하며 최종 후보 JSON은 32KB로 제한합니다.
2. LLM은 `dataset_key`만 선택합니다. `source_type`, SQL, endpoint 같은 실행 설정은 active table catalog에서 deterministic하게 주입합니다.
3. 조회 분기에는 전체 main payload가 아니라 선택된 job bundle과 최소 request context만 전달합니다.
4. 전체 helper library 대신 실제 선택된 함수 정의만 pandas prompt에 전달합니다.
5. pandas 실행 노드는 하나만 두고, 최초 실행이 실제로 실패한 경우에만 이전 생성 코드와 실행 오류를 Repair LLM에 전달해 정확히 한 번 수정·재실행합니다. Repair Prompt 원문은 canvas의 `17B pandas 복구 프롬프트 템플릿` Text Input에서 직접 확인·편집하며, 오류 문맥 치환과 모델 호출은 실패 시점에 executor 내부에서 수행합니다. 최초 성공 시 Repair LLM은 호출하지 않으며 최종 payload와 Chat Output은 하나로 유지합니다.
6. row는 `data.rows`, 실행 코드는 `trace.inspection.pandas_execution.generated_code`, 사용자 메시지는 API의 `message`가 각각 단일 소유합니다.
7. 세션 상태 저장기를 최종 Message/API 경로의 필수 선행 노드로 연결해 저장이 병렬 side effect로 빠지지 않게 했습니다.
8. `WORK_DT`, `WORK_DATE` 등 날짜 의미 컬럼은 `20200625`처럼 숫자로 보여도 `YYYYMMDD` 문자열로 보존하고 수량형 숫자로 변환하지 않도록 pandas 생성 프롬프트에 고정했습니다.
9. 의도 분석의 `analysis_kind`는 현재 dataset·metric·grouping을 반영한 구체적인 이름을 사용하며, 제품별 계획 질문은 `target_plan_by_product`로 분류하도록 프롬프트 계약을 명시했습니다.
10. pandas 코드는 `pd`와 `Series.where`/`mask`/`fillna`를 우선 사용합니다. LLM이 정확한 단독 구문 `import pandas as pd` 또는 `import numpy as np`를 생성해도 executor가 실제 import를 실행하지 않고 해당 줄을 제거한 뒤 `pd` 또는 제한형 `np` 계산 namespace를 주입합니다. 다른 import와 pandas/numpy 파일·네트워크 I/O API는 계속 차단합니다.
11. Router를 제외한 모든 Flow는 Chat Output을 하나만 사용합니다. 저장 Flow 3종은 dry/live 그래프 분기와 두 번째 review LLM을 제거하고 단일 Writer 경로로 통합했습니다.
12. 저장 Flow는 기존 v4 문서를 `registration_trace`만 제외해 Matcher로 직접 전달하며, Domain은 같은 section의 exact key와 유일한 normalized key/alias/display_name identity를 canonical target으로 사용합니다. `replace`에서 유사 항목이 있으면 교체하고 없으면 신규 저장하며, 복수 target으로 모호할 때만 저장하지 않습니다. 기존 전체 문서는 Request나 LLM prompt에 전달하지 않습니다.
13. standalone 실행을 대기·재개하지 못하는 `ask` 중복 모드는 제거했습니다. 기본값은 기존 문서를 보존하는 `skip`이며, 기존 `ask` 입력도 `skip`으로 안전하게 정규화합니다.
14. Metadata QA와 저장 Flow의 MongoDB 입력에는 `datagov` 및 각 `agent_v4_*` collection 기본값을 명시합니다. `mongo_uri`는 비밀값이므로 계속 비워 두고 환경변수로 제공합니다.
15. API Router의 `direct_answer`와 `clarification`은 예전 정상 Flow와 동일하게 Smart Router에서 각 Chat Output으로 직접 연결합니다. 추가 terminal Gate는 질문 JSON 카드를 별도로 표시하므로 제거했습니다. 또한 Chat Input은 Smart Router에만 연결하고 API caller 5개로 향하던 `session_source` fan-out edge를 제거했습니다. Langflow가 caller의 `session_id` 입력에 부모 세션을 자동 주입하므로 세션은 유지하면서 Smart Router 재빌드와 질문 Message 반복을 막습니다.
16. Router 하위 Flow read timeout은 240초, 외부 Web/API client 기본 timeout은 300초입니다. timeout 상향은 장기 실행을 실패로 오판하지 않기 위한 여유이며 실행시간 자체를 줄이는 최적화는 아닙니다.
17. pandas 안전 실행 namespace에 `zip`을 명시적으로 제공하고 최초/repair 프롬프트에 같은 builtin 계약을 노출해 `dict(zip(...))`가 불필요한 1회 repair를 유발하지 않도록 했습니다. 기존 오류 시 최대 1회 repair 계약은 그대로 유지합니다.
18. 운영 기본 Router는 결정된 API 방식이며 Native Run Flow 노드가 없습니다. API caller 5개는 240초 read timeout과 원본 session 전달을 유지합니다.
19. 별도 `Agent + Tool Mode Router`를 추가했습니다. 이름 기반 Tool 5개는 import 후 실제 Flow ID를 다시 해석하고 `cache_flow=true`로 그래프만 캐시합니다. Tool schema에는 node ID가 없는 필수 `question` 하나만 포함하고, 실행 직전에 현재 하위 Flow의 단일 Chat Input으로 변환합니다. `return_direct=true`로 추가 Agent 재작성을 생략하며, 각 Tool은 부모 `graph.session_id`를 자동 상속합니다.

## 검증 상태와 현재 제약

- 이 작업 환경에서는 실제 Oracle/H-API/Datalake/Goodocs 자격증명과 원천 데이터가 없어 dummy 경로로 검증했습니다.
- 제공 예시 질문 23개는 trusted catalog hydration, 선택 helper, pandas 실행, 답변/API adapter를 포함한 deterministic dummy 경로에서 23/23 통과했습니다. 기존 13개뿐 아니라 target·장비·UPH·LOT/HOLD·0건·다중 source 질문도 포함합니다.
- 전체 pytest 222개와 대표 dummy 질문 23/23이 통과했습니다.
- 이 PC의 Langflow Desktop 런타임(`langflow 1.8.2`, `lfx 0.3.4`)에서 현재 7개 Flow의 115/115 node template build와 격리 서버 7/7 JSON import(HTTP 201)를 확인했습니다. Agent Tool Router는 실제 import로 새로 발급된 하위 Flow ID를 이름으로 찾고 `CachedFlowTool-data_analysis`까지 partial build에 성공했습니다.
- 실제 문제 실행 기록에서는 기존 06 Router의 session fan-out 때문에 ChatInput/SmartRouter가 각각 2회 빌드되고 비선택 direct/clarification Chat Output이 질문을 두 번 저장한 사실을 확인했습니다. 수정 JSON은 Chat Input outgoing edge를 Smart Router 한 개로 제한하며, 운영 provider를 사용한 최종 화면 재검증은 새 06을 import한 뒤 수행합니다.
- 격리 Langflow 서버에는 `GOOGLE_API_KEY` Global Variable이 없어 Agent/LLM을 포함한 전체 Flow 실행은 수행하지 않았습니다. 운영 인스턴스에서는 같은 이름의 Global Variable 또는 회사 표준 provider 설정이 필요합니다.
- 운영 전에는 Data Analysis Flow의 단일 설정인 `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode=live`로 전환한 뒤 source별 최소 한 건 smoke test와 2-turn 후속질문 검증이 필요합니다. `07`에는 별도 모드 설정이 없습니다.

## 한글 소스 설명과 JSON 동기화

- `langflow_components`의 Python 68개에는 역할·입력·출력·처리 흐름·유지보수 포인트와 전체 함수 1000/1000의 인접 한글 설명이 들어 있습니다. private helper, 클래스 메서드, async 함수와 중첩 함수도 포함합니다.
- JSON 문법은 구조 주석을 허용하지 않으므로, 한글 설명은 각 Custom Component의 `template.code.value`에 Python 주석으로 포함됩니다. Langflow 코드 편집기에서 원본과 동일하게 확인할 수 있습니다.
- `.editorconfig`와 각 Python 파일 첫 줄의 UTF-8 선언으로 Windows 편집기의 인코딩 오저장을 예방합니다.
- `python tools/add_korean_component_comments.py --check`와 `python tools/validate_korean_component_documentation.py`로 함수별 설명 누락·BOM·깨짐 문자·JSON 내장 코드·ZIP을 재검증할 수 있습니다. 자동 설명 규칙을 개선한 뒤에는 `--refresh-functions`로 기존 자동 주석을 갱신할 수 있습니다.
- 자세한 적용 범위와 검증 결과는 `docs/KOREAN_COMPONENT_DOCUMENTATION_20260712.md`를 참고하세요.
