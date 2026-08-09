# metadata_driven_v5

Langflow standalone 환경에서 실행하는 메타데이터 기반 제조 데이터 분석 에이전트입니다. 등록된 Domain, Table Catalog, Main Flow Filter를 근거로 질의를 해석하고, Fast 고정 실행 또는 Complex pandas 분석으로 결과를 만듭니다.

기준 런타임은 **Langflow 1.9.2 / langflow-base 0.9.2 / LFX 0.4.2 / Python 3.12**입니다.

## 현재 지원 Flow

| 번호 | Flow | 역할 |
| ---: | --- | --- |
| 01 | `v5_data_analysis` | 일반 메타데이터 기반 데이터 조회·분석. Fast/Complex Hybrid 실행 |
| 02 | `v5_domain_saving` | 작업자 자연어 Domain 설명 저장 |
| 03 | `v5_table_catalog_saving` | 실제 데이터셋·컬럼·조회 조건 Catalog 저장 |
| 04 | `v5_main_flow_filter_saving` | 공통 필터 규칙 저장 |
| 05 | `v5_metadata_qa` | 등록된 메타데이터 질의응답 |
| 06 | `v5_agent_tool_router` | 일반 질의를 01 또는 지원 Tool로 라우팅 |
| 07 | `v5_realtime_production_report` | 정해진 실시간 생산 Report 생성 |
| 08 | `v5_data_analysis_continuation` | 상위 결과를 이용한 최대 2단계 종속 조회 |
| 09 | `v5_agent_tool_router_continuation` | 08을 선택적으로 호출하는 Router |

`01`은 기본 Data Analysis Flow입니다. `08/09`는 첫 조회 결과의 식별자를 두 번째 조회 조건으로 전달해야 하는 경우에만 사용합니다.

## Import-ready Flow

- 전체 9개 Flow: [00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json](import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json)
- 개별 Flow와 import 방법: [README_IMPORT.md](import_ready_flows/README_IMPORT.md)
- 기본 Data Analysis: [01_data_analysis_flow_v2_standalone.json](import_ready_flows/01_data_analysis_flow_v2_standalone.json)
- Continuation Data Analysis: [08_data_analysis_flow_v2_continuation_standalone.json](import_ready_flows/08_data_analysis_flow_v2_continuation_standalone.json)

Import 후에는 Langflow Provider 설정과 `MONGO_URL` Credential Global Variable을 설정합니다. 이미 저장된 Router의 `flow_id_selected`가 있으면, 해당 Tool의 대상 Flow를 한 번 다시 선택해 현재 import Flow ID로 갱신합니다.

## 답변 표시 원칙

`21 V2 답변 메시지 어댑터`는 운영자가 필요한 정보만 선택해 표시합니다.

- `중간 결과 표시`: 유지합니다. 정상 완료 시에는 최종 집계 전 중간 데이터, 오류 시에는 마지막 정상 중간 데이터를 고정 표와 다운로드 링크로 표시합니다.
- `중간 산출물/helper 결과 표시`: 제거했습니다. 내부 step/helper trace는 본문에 노출하지 않습니다.
- `다음 질문을 답변 본문에도 표시`: 제거했습니다. 연관 질문은 본문 중복 없이 Message metadata로만 전달합니다.
- `중간 결과 미리보기 행 수`: 1~5 범위에서 조정할 수 있으며, LLM 입력 토큰에는 포함되지 않습니다.

## 필요한 서버

현재 운영 서버는 **Artifact Server 하나**입니다. 분석 결과 CSV/JSON 다운로드와 Flow 07의 HTML Report 저장·보기·다운로드를 제공합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python -m artifact_server
```

기본 주소는 `http://127.0.0.1:8765`입니다. `ARTIFACT_LISTEN_HOST`, `ARTIFACT_LISTEN_PORT`, `ARTIFACT_PUBLIC_BASE_URL`은 [.env.example](.env.example)에서 설정합니다.

## Flow 재생성

```powershell
python tools\build_v5_auxiliary_flows.py
python tools\build_data_analysis_flow_v2.py
python tools\build_data_analysis_flow_v2_continuation.py
python tools\build_agent_tool_router_continuation.py
python tools\build_continuation_import_ready_bundle.py
```

## 검증

```powershell
python -m pytest tests/test_data_analysis_flow_v2.py tests/test_v5_flow_export.py -q --basetemp=.pytest-tmp
python tools\validate_flow_component_sources.py
python tools\validate_langflow_runtime.py
```

실제 Provider·MongoDB·원천 데이터 연결이 필요한 검증은 해당 운영 환경의 인증정보와 네트워크가 준비된 뒤 수행합니다. 연결 실패 시에는 모델이 데이터셋을 추측하지 않고, 메타데이터 연결 또는 등록 상태를 오류 원인으로 반환하도록 설계되어 있습니다.

## 핵심 설계

1. **메타데이터 우선**: 모델은 등록된 후보 안에서만 dataset·조건·분석 의도를 선택합니다. 실행 설정·실제 컬럼 바인딩은 Catalog 기반으로 결정합니다.
2. **Fast/Complex 분기**: 단일 데이터셋의 조회·집계·정렬·상하위·최대/최소/개수는 고정 실행으로 처리하고, 조인·복합 계산은 제한된 pandas 경로로 처리합니다.
3. **계약 기반 안전성**: 조회 schema, 필수 컬럼, output contract를 실행 전에 확인합니다. 계약 오류가 나도 마지막 정상 중간 결과와 다운로드 참조를 남깁니다.
4. **종속 조회**: 08은 Typed continuation 계약과 결과 참조를 이용해 같은 세션에서 한 번만 이어 조회합니다. 첫 단계 답변 LLM과 두 번째 단계 Intent LLM은 생략합니다.
5. **재사용성**: 업무별 Python 조건문 대신 Domain, Table Catalog, Main Flow Filter와 Typed IR primitive로 동작하도록 구성했습니다.

세부 설치 기준은 [LANGFLOW_1_9_2_MIGRATION.md](docs/LANGFLOW_1_9_2_MIGRATION.md), 현재 Flow·서버 운영 설명은 [ACTIVE_FLOWS_AND_RUNTIME.md](docs/ACTIVE_FLOWS_AND_RUNTIME.md)를 참고하세요.
