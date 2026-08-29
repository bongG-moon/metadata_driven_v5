# metadata_driven_v5 import-ready bundle

이 bundle은 현재 지원하는 **9개 Flow(01~06, 07, 07-1, 07-2)**를 포함합니다. 모두 Langflow 1.11.0 / langflow-base 0.11.0 / LFX 1.11.0 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 번호 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 01 | `01_data_analysis_flow_v2_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis` | 52 | 61 |
| 02 | `02_domain_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-domain-saving` | 12 | 13 |
| 03 | `03_table_catalog_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-table-catalog-saving` | 12 | 13 |
| 04 | `04_main_flow_filter_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-main-flow-filter-saving` | 12 | 13 |
| 05 | `05_metadata_qa_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-metadata-qa` | 13 | 20 |
| 06 | `06_agent_tool_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router` | 13 | 21 |
| 07 | `07_realtime_production_report_legacy_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-realtime-production-report-legacy` | 9 | 11 |
| 07-1 | `07_1_realtime_production_report_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-realtime-production-report` | 11 | 15 |
| 07-2 | `07_2_report_followup_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-report-followup` | 11 | 13 |

## 운영 설정

- Langflow 모델 Provider와 `MONGO_URL` Credential Global Variable을 import 후 설정합니다.
- 모든 일반 Data Analysis와 일반 분석 후속 질문은 `01. v5_data_analysis`가 담당합니다.
- `07-2. v5_report_followup`은 독립 검증·개발용 artifact로 보존하지만, 현재 Flow 06 Router의 Tool 목록에는 노출하지 않습니다. Report 후속 분석을 운영 경로로 채택하기 전까지 관련 질문은 일반 `01. v5_data_analysis` 또는 필요한 확인 질문으로 처리합니다.
- `07. v5_realtime_production_report_legacy`는 변경 전 직접 응답 구조를 보존한 호환 Flow이며 Router가 자동 선택하지 않습니다. `07-1. v5_realtime_production_report`는 현재 Router 대상 Report입니다.
- Flow 06은 `datagov.router_session_states`에 직전 사용자 질문, 직전 최종 답변, 직전 선택 Flow 한 세트만 저장합니다. 이는 Flow 01의 `agent_v4_session_states`와 별도 컬렉션이며, MongoDB 또는 외부 문맥이 없으면 현재 질문만으로 기존 단일턴 Router 경로를 계속 실행합니다. 외부 GAIA 실행에서는 `tweaks["GaiA Input"]`의 `data`와 `metadata`를 사용합니다. `GaiA Input`은 `metadata.session_id`만 담은 전용 출력으로 `00 Router 세션 문맥 로더`의 `외부 세션 ID`에 연결하므로, 사용자 질문이 세션 ID로 오인되지 않습니다.
- 결과 CSV/JSON 다운로드와 실시간 Report HTML 발행은 API_SERVER(`python API_SERVER\app.py`, bind `0.0.0.0:5000`)가 담당합니다. Report HTML과 메타데이터는 API_SERVER의 단일 MongoDB 컬렉션에 저장되므로 Flow의 Report API 주소를 접근 가능한 API URL로 설정합니다.
- Flow 01의 `25 분석 처리 과정 HTML 발행기`는 분석·저장·세션 처리 뒤에 best-effort로 실행됩니다. API_SERVER가 일시적으로 사용할 수 없으면 분석 결과는 그대로 반환되고 HTML 링크만 생략됩니다. 기본 링크 유효시간은 1시간, 발행 요청 제한은 2초입니다.
- 기존 Router Tool에 저장된 `flow_id_selected`가 있으면, import 뒤 대상 Flow를 한 번 다시 선택해 현재 Flow ID로 갱신합니다.

## 생성 시 구조 검증

- Flow 06에만 GaiA Input ingress adapter 1개, 모든 Flow에 native Chat Input/Chat Output 각각 1개
- 모든 node `lf_version=1.11.0`
- edge handle 360/360, custom component template 135/135
