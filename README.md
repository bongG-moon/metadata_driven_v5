# metadata_driven_v5

Langflow standalone 환경에서 실행하는 메타데이터 기반 제조 데이터 분석 에이전트입니다. 등록된 Domain, Table Catalog, Main Flow Filter를 근거로 질의를 해석하고, Fast 고정 실행 또는 Complex pandas 분석으로 결과를 만듭니다.

기준 런타임은 **Langflow 1.11.0 / langflow-base 0.11.0 / LFX 1.11.0 / Langflow Desktop Python 3.13**입니다.

## 현재 지원 Flow

| 번호 | Flow | 역할 |
| ---: | --- | --- |
| 01 | `v5_data_analysis` | 일반 메타데이터 기반 데이터 조회·분석. Fast/Complex Hybrid 실행 |
| 02 | `v5_domain_saving` | 작업자 자연어 Domain 설명 저장 |
| 03 | `v5_table_catalog_saving` | 실제 데이터셋·컬럼·조회 조건 Catalog 저장 |
| 04 | `v5_main_flow_filter_saving` | 공통 필터 규칙 저장 |
| 05 | `v5_metadata_qa` | 등록된 메타데이터 질의응답 |
| 06 | `v5_agent_tool_router` | 일반 질의를 01 또는 지원 Tool로 라우팅 |
| 07 | `v5_realtime_production_report_legacy` | 후속 Context가 없던 변경 전 Report 구조를 1.11에서 보존 |
| 07-1 | `v5_realtime_production_report` | 실시간 생산 Report 생성 및 후속 분석용 Snapshot Context 발행 |
| 07-2 | `v5_report_followup` | 같은 세션의 Report Snapshot을 전용 View 계약으로 후속 조회 |

운영 진입점은 `06`입니다. 모든 일반 데이터 분석은 `01`, Report 생성은 `07-1`, 같은 세션의 Report Snapshot 후속 질문은 전용 `07-2`를 사용합니다. `07-1`이 Report 생성 당시의 전체 판정 Snapshot과 후속 조회용 Materialized View를 저장하므로, `그중`, `방금 Report`, `위 결과` 같은 후속 질문은 원천 DB를 다시 조회하지 않습니다. `현재 기준`, `최신 데이터`, `다시 조회` 또는 다른 데이터셋 결합 요청은 `01`의 신규 조회 경로로 보냅니다. `07`은 변경 전 Report 응답 구조를 보존한 직접 실행용 Flow이며 Router가 자동 선택하지 않습니다.

## Import-ready Flow

- 전체 9개 Flow: [00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json](import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json)
- 개별 Flow와 import 방법: [README_IMPORT.md](import_ready_flows/README_IMPORT.md)
- 기본 Data Analysis: [01_data_analysis_flow_v2_standalone.json](import_ready_flows/01_data_analysis_flow_v2_standalone.json)
- 변경 전 Report: [07_realtime_production_report_legacy_flow_v5_standalone.json](import_ready_flows/07_realtime_production_report_legacy_flow_v5_standalone.json)
- 기존 저장 Flow와 분리된 rev_2 3종: [rev_2/README_IMPORT.md](import_ready_flows/rev_2/README_IMPORT.md)

Import 후에는 Langflow Provider 설정과 `MONGO_URL` Credential Global Variable을 설정합니다. 이미 저장된 Router의 `flow_id_selected`가 있으면, 해당 Tool의 대상 Flow를 한 번 다시 선택해 현재 import Flow ID로 갱신합니다.

## 답변 표시 원칙

`21 V2 답변 메시지 어댑터`는 운영자가 필요한 정보만 선택해 표시합니다.

- `중간 결과 표시`: 유지합니다. 정상 완료 시에는 최종 집계 전 중간 데이터, 오류 시에는 마지막 정상 중간 데이터를 고정 표와 다운로드 링크로 표시합니다.
- `중간 산출물/helper 결과 표시`: 제거했습니다. 내부 step/helper trace는 본문에 노출하지 않습니다.
- `다음 질문을 답변 본문에도 표시`: 제거했습니다. 연관 질문은 본문 중복 없이 Message metadata로만 전달합니다.
- `중간 결과 미리보기 행 수`: 1~5 범위에서 조정할 수 있으며, LLM 입력 토큰에는 포함되지 않습니다.

## 필요한 서버

현재 Flow JSON의 기본 설정은 **API_SERVER 하나**를 사용합니다. 분석 결과 CSV/JSON 다운로드와 Flow 07·07-1의 HTML Report 저장·보기·다운로드를 함께 제공합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python API_SERVER\app.py
```

기본 주소는 `http://127.0.0.1:5000`입니다. 배포 주소와 MongoDB 설정은 [API_SERVER/.env.example](API_SERVER/.env.example)을 기준으로 설정합니다.

## Flow 재생성

```powershell
$lf = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Scripts\python.exe"
& $lf tools\build_v5_auxiliary_flows.py
& $lf tools\build_data_analysis_flow_v2.py
& $lf tools\build_import_ready_bundle.py
& $lf tools\build_metadata_saving_rev_2_flows.py
```

## 검증

```powershell
$lf = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Scripts\python.exe"
& $lf -m pytest tests/test_data_analysis_flow_v2.py tests/test_v5_flow_export.py tests/test_metadata_saving_rev_2.py -q --basetemp=.pytest-tmp
& $lf tools\validate_flow_component_sources.py
& $lf tools\validate_langflow_runtime.py --all-flows
```

`--all-flows`는 기존 canonical 9개 export를 검사합니다. 저장 Flow rev_2는 별도 파일 3개를 `--flow flow_exports\rev_2\...json`으로 각각 검사합니다. 설계·입력/응답 예시와 운영 전환 조건은 [METADATA_SAVING_REV_2.md](docs/METADATA_SAVING_REV_2.md)를 참고하세요.

실제 Provider·MongoDB·원천 데이터 연결이 필요한 검증은 해당 운영 환경의 인증정보와 네트워크가 준비된 뒤 수행합니다. 연결 실패 시에는 모델이 데이터셋을 추측하지 않고, 메타데이터 연결 또는 등록 상태를 오류 원인으로 반환하도록 설계되어 있습니다.

## 핵심 설계

1. **메타데이터 우선**: 모델은 등록된 후보 안에서만 dataset·조건·분석 의도를 선택합니다. 실행 설정·실제 컬럼 바인딩은 Catalog 기반으로 결정합니다.
2. **Fast/Complex 분기**: 단일 데이터셋의 조회·집계·정렬·상하위·최대/최소/개수는 고정 실행으로 처리하고, 조인·복합 계산은 제한된 pandas 경로로 처리합니다.
3. **계약 기반 안전성**: 조회 schema, 필수 컬럼, output contract를 실행 전에 확인합니다. 계약 오류가 나도 마지막 정상 중간 결과와 다운로드 참조를 남깁니다.
4. **Report 후속 분석**: 07-1은 HTML을 데이터 소스로 사용하지 않고 Report 생성 당시 전체 Snapshot과 조회용 View를 공용 Result Store에 저장합니다. 07-2는 같은 세션·만료·저장 완전성을 확인한 뒤 선언된 View만 복원하며, 명시적 최신·재조회·교차 데이터 요청은 01로 보냅니다. context 오류나 조회 위임 상태에서는 07-2의 계획 LLM도 호출하지 않습니다.
5. **Report 호환성**: 07은 후속 Context 저장 이전의 9-node/11-edge Report를 그대로 보존합니다. 자동 Router에는 연결하지 않아 현재 경로와 의도가 충돌하지 않습니다.
6. **재사용성**: 업무별 Python 조건문 대신 Domain, Table Catalog, Main Flow Filter와 Typed IR primitive로 동작하도록 구성했습니다.

세부 설치 기준은 [LANGFLOW_1_11_MIGRATION.md](docs/LANGFLOW_1_11_MIGRATION.md), 현재 Flow·서버 운영 설명은 [ACTIVE_FLOWS_AND_RUNTIME.md](docs/ACTIVE_FLOWS_AND_RUNTIME.md)를 참고하세요.
