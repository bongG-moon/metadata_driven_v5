# metadata_driven_v5 import-ready bundle

이 bundle은 현재 지원하는 **9개 Flow(01~07, 10, 11)**를 포함합니다. 모두 Langflow 1.11.0 / langflow-base 0.11.0 / LFX 1.11.0 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 1 | `01_data_analysis_flow_v2_standalone.json` | `metadata-driven-v5-complete-20260710-data-analysis` | 51 | 59 |
| 2 | `02_domain_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-domain-saving` | 12 | 13 |
| 3 | `03_table_catalog_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-table-catalog-saving` | 12 | 13 |
| 4 | `04_main_flow_filter_saving_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-main-flow-filter-saving` | 12 | 13 |
| 5 | `05_metadata_qa_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-metadata-qa` | 11 | 17 |
| 6 | `06_agent_tool_router_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-agent-tool-router` | 11 | 10 |
| 7 | `07_realtime_production_report_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-realtime-production-report` | 12 | 17 |
| 10 | `10_report_followup_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-report-followup` | 11 | 13 |
| 11 | `11_realtime_production_report_legacy_flow_v5_standalone.json` | `metadata-driven-v5-complete-20260710-realtime-production-report-legacy` | 9 | 11 |

## 운영 설정

- Langflow 모델 Provider와 `MONGO_URL` Credential Global Variable을 import 후 설정합니다.
- 모든 일반 Data Analysis와 일반 분석 후속 질문은 `01. v5_data_analysis`가 담당합니다.
- 같은 세션의 Report Snapshot 또는 Report가 미리 만든 집계 View에 대한 컬럼 선택·필터·정렬·순위는 `10. v5_report_followup`이 담당합니다. 새 groupby 계산, 최신 데이터나 다른 데이터셋이 필요한 질문은 `01. v5_data_analysis`로 보냅니다.
- `07. v5_realtime_production_report`는 후속분석 Context를 저장하는 현재 기본 Report입니다. `11. v5_realtime_production_report_legacy`는 변경 전 직접 응답 구조를 보존한 호환 Flow이며 Router가 자동 선택하지 않습니다.
- 결과 CSV/JSON 다운로드와 실시간 Report HTML 발행은 API_SERVER(`python API_SERVER\app.py`, bind `0.0.0.0:5000`)가 담당합니다. Report HTML과 메타데이터는 API_SERVER의 단일 MongoDB 컬렉션에 저장되므로 Flow의 Report API 주소를 접근 가능한 API URL로 설정합니다.
- 기존 Router Tool에 저장된 `flow_id_selected`가 있으면, import 뒤 대상 Flow를 한 번 다시 선택해 현재 Flow ID로 갱신합니다.

## 생성 시 구조 검증

- GaiA Input/Output boundary node 없음
- 각 Flow의 native Chat Input/Chat Output 각각 1개
- 모든 node `lf_version=1.11.0`
- edge handle 332/332, custom component template 131/131
