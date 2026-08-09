# metadata_driven_v5 import-ready bundle

이 bundle에는 현재 지원하는 **9개 Flow**가 모두 포함되어 있습니다. 모든 Flow는 Langflow 1.9.2 / langflow-base 0.9.2 / LFX 0.4.2 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 1 | `01_data_analysis_flow_v2_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis` | 51 | 59 |
| 2 | `02_domain_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-domain-saving` | 12 | 13 |
| 3 | `03_table_catalog_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-table-catalog-saving` | 12 | 13 |
| 4 | `04_main_flow_filter_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-main-flow-filter-saving` | 12 | 13 |
| 5 | `05_metadata_qa_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-metadata-qa` | 11 | 17 |
| 6 | `06_agent_tool_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router` | 9 | 8 |
| 7 | `07_realtime_production_report_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-realtime-production-report` | 9 | 11 |
| 8 | `08_data_analysis_flow_v2_continuation_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis-continuation` | 56 | 67 |
| 9 | `09_agent_tool_router_continuation_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router-continuation` | 9 | 8 |

## 실행 범위

- 기본 분석은 `01. v5_data_analysis`입니다.
- 현재 분석 결과를 다음 조회 조건으로 넘기는 경우에만 `08. v5_data_analysis_continuation`과 `09. v5_agent_tool_router_continuation`을 사용합니다.
- `06. v5_agent_tool_router`와 `09. v5_agent_tool_router_continuation`에 이전 import의 `flow_id_selected`가 남아 있다면 현재 Flow를 다시 선택합니다.
- CSV/JSON 다운로드와 Flow 07 HTML Report 발행은 Artifact Server(`python -m artifact_server`, 기본 `127.0.0.1:8765`)가 담당합니다.

## 생성 및 구조 검증

- GaiA Input/Output boundary node 없음; 각 Flow는 native Chat Input/Chat Output을 각각 하나씩 사용
- 모든 node `lf_version=1.9.2`
- edge handle 418/418
- custom component template 160/160
