# Current Version Cleanup Report

현재 배포 기준은 7개 standalone Flow와 `route_flow`(06 API Router), `route_flow_v2`(07 Agent + Tool Router)입니다. 이번 정리는 실제 빌더·테스트·JSON 참조를 확인한 뒤 현재 경로와 무관한 이전 구현만 제거했습니다.

## 제거한 실행 소스

- Data Analysis의 옛 분리형 repair gate/variables/selector 3개
- Domain/Table Catalog/Main Flow Filter Saving의 옛 text-refinement/review 컴포넌트 12개
- 위 Saving 분기의 사용하지 않는 prompt 6개
- import되지 않고 존재하지 않는 `reference_runtime.agent`를 참조하던 `web_app/mock_api.py`
- Web client의 제거된 별도 Dummy Flow, report generation, operations diagnosis route 호환 코드

현재 pandas 오류 복구는 `17_pandas_code_executor.py`가 최초 코드와 오류 문맥을 사용해 최대 한 번만 수행합니다. Metadata Saving은 추출 Agent 1회와 단일 결정론 Writer 경로를 사용합니다. Dummy 검증은 별도 Flow가 아니라 Data Analysis 내부 `retrieval_mode=dummy`입니다.

## 제거한 이전 문서

- metadata-driven v2/v3 delivery/readiness/rebuild 문서
- 제거된 Native Run Flow와 Dummy Router 연결 계획
- 구현 전 external audit snapshot과 완료된 중간 implementation plan/checkpoint
- 현재 존재하지 않는 regression metadata 파일을 가리키던 validation 문서

현재 사용 문서는 환경 설정, Flow 연결, payload 계약, Metadata Saving, Web 구현, 검증 결과와 v5 구현 보고서 중심으로 남겼습니다.

## 현재 구현에 필요해 보존한 항목

- `flow_exports/data_analysis_flow_v4_reference.json`: `build_v5_data_analysis_flow.py`의 실제 donor
- `agent_v4_*` MongoDB collection 이름: 기존 v4 데이터베이스를 그대로 공유하기로 한 운영 계약
- `function_case_helper_code_input_example.py`: 15A 선택 helper 생성기의 실제 라이브러리
- `08_dummy_data_retriever.py`: 단일 Data Analysis Flow의 현재 dummy 조회 경로
- `reference_runtime`, Web app, 7개 Flow export/import-ready JSON과 ZIP

## 재발 방지

- source validator는 67개 활성 Custom Component 원본과 1개 지원 helper만 허용하고 비활성 Python이 다시 생기면 실패합니다.
- 테스트는 과거 분리형 repair가 아니라 executor 내부 1회 repair를 직접 검증합니다.
- Router 자동 세션 접두사는 `route_flow_`를 사용하며 `router_v3_` 표기를 제거했습니다.
- `.gitignore`는 Python/pytest/coverage/IDE/virtualenv/build/SQLite/log 산출물을 제외합니다.

## 최종 검증

- 전체 pytest: 221/221 통과
- 대표 Data Analysis dummy 질문: 23/23 통과
- source validator: 활성 Custom Component 67개, 지원 helper 1개, 비활성 Python 0개
- Langflow 1.8.2 / LFX 0.3.4 node template: 115/115 통과
- 격리 Langflow import: 7/7 HTTP 201
- 07 `CachedFlowTool-data_analysis` partial build: 성공
