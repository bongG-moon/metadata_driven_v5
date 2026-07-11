# Custom Component Source Audit

검토 대상은 현재 배포하는 7개 standalone Flow, 개별 import-ready JSON 7종, 통합 import JSON입니다.

## 결론

- 로컬 커스텀 노드 인스턴스 75개는 모두 `langflow_components/**/*.py` 원본을 가집니다.
- 고유 커스텀 Python 원본은 67개이며 JSON에만 존재하는 커스텀 코드는 없습니다.
- `flow_exports`, 개별 import-ready JSON, 통합 import JSON의 노드-원본 매핑은 같습니다.
- Langflow 기본 노드인 Chat Input/Output, Agent, Prompt Template, Text Input, Smart Router는 `lfx`가 제공하므로 이 저장소에 별도 `.py`를 복제하지 않는 것이 정상입니다.

## Flow별 원본 현황

| 번호 | Flow | 커스텀 노드 인스턴스 | 고유 `.py` 원본 |
| --- | --- | ---: | ---: |
| 01 | Data Analysis | 29 | 29 |
| 02 | Domain Saving | 9 | 9 |
| 03 | Table Catalog Saving | 9 | 9 |
| 04 | Main Flow Filter Saving | 9 | 9 |
| 05 | Metadata QA | 9 | 9 |
| 06 | API Router (`route_flow`) | 5 | 1 |
| 07 | Agent + Tool Router (`route_flow_v2`) | 5 | 1 |
| 합계 |  | 75 | 67 |

06의 API 호출기 5개는 `langflow_components/route_flow/01_flow_api_message_caller.py` 하나를 route별 설정으로 재사용합니다. 07의 Tool 5개도 `langflow_components/route_flow_v2/01_cached_named_run_flow_tool.py` 하나를 대상 Flow와 Tool 설명만 달리해 재사용합니다.

## Router 소스 정리

현재 기준은 아래 두 폴더뿐입니다.

```text
langflow_components/
  route_flow/       # 06 Smart Router + 하위 Flow API 호출
  route_flow_v2/    # 07 Agent + Cached Run Flow Tool
```

과거 `router_flow`, `router_flow_v2`, `router_flow_v3`, `router_tool_flow` 폴더는 제거했습니다. 공개 JSON 파일명과 endpoint는 기존 연동을 보호하기 위해 변경하지 않았습니다.

## 비활성 구현 정리

전체 Python 원본은 68개입니다. 현재 7개 Flow가 사용하는 Custom Component 원본은 67개이고, 나머지 1개인 `function_case_helper_code_input_example.py`는 15A가 선택된 helper 정의를 추출할 때 사용하는 지원 라이브러리입니다. 현재 Flow와 무관한 비활성 Python은 없습니다.

이전 분리형 pandas repair 컴포넌트 3개와 Metadata Saving의 옛 text-refinement/review 컴포넌트 12개, 대응 prompt 6개는 현재 통합 executor·단일 Writer 계약에 맞춰 제거했습니다. 회귀 테스트와 대표 질문 검증기도 현재 실행 경로를 직접 검증하도록 변경했습니다.

## 지속 검증

아래 명령은 세 JSON 계층을 모두 읽고 각 로컬 커스텀 노드의 임베디드 코드와 `.py` 원본, `code_hash`, 06/07 폴더 계약을 검증합니다.

```powershell
python tools/validate_flow_component_sources.py
```

검증 실패 조건은 원본 누락, 같은 코드 원본의 중복, 코드 해시 불일치, export/import-ready/통합 bundle 매핑 불일치, 과거 Router 폴더 재등장입니다.

## 2026-07-12 실행 결과

- source validator: 3개 JSON 계층 모두 75/75 커스텀 노드 매핑 성공, 오류 0
- 전체 Python 계약 테스트: 221/221 통과
- 대표 Data Analysis dummy 질문: 23/23 통과
- Langflow 1.8.2 / LFX 0.3.4 node template: 115/115 통과
- 격리 Langflow 서버 import: 7/7 HTTP 201
- 07 `CachedFlowTool-data_analysis` partial build: 성공
