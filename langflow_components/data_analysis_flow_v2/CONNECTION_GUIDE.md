# Data Analysis Flow V2 연결 가이드

## 목적

`data_analysis_flow_v2`는 기존 Data Analysis Flow의 메타데이터 조회, 의도 정규화, trusted catalog hydration, source 조회, 결과 저장, 다운로드, 세션 상태 및 API 응답 계약을 그대로 사용합니다. 차이는 조회 이후 단일 source 분석을 결정론적 Fast Path로 실행할 수 있다는 점입니다.

기존 `01. v5_data_analysis`는 수정하거나 대체하지 않습니다. V2는 `12. data_analysis_flow_v2`와 `metadata-driven-v5-complete-20260710-data-analysis-v2`라는 별도 Flow/endpoint로 import됩니다.

## 핵심 연결

```text
14A 필수 조회 실행 게이트
  → 14B V2 단순 분석 계약 결정기
      → 15 pandas 변수 생성기
      → 17 V2 Hybrid 분석 실행기

15 pandas 변수 생성기 → pandas Prompt Template
pandas Prompt Template → 17 V2 Hybrid 분석 실행기.pandas_prompt

17 V2 Hybrid 분석 실행기 → 23 MongoDB 결과 저장소
23 MongoDB 결과 저장소 → 18 답변 생성 변수 생성기 → 답변 Prompt Template
답변 Prompt Template → 20 V2 Hybrid 답변 생성기.answer_prompt
23 MongoDB 결과 저장소 → 20 V2 Hybrid 답변 생성기.payload
```

외부의 항상 실행되는 pandas/answer Language Model 노드는 없습니다. Hybrid 컴포넌트가 `simple_analysis_contract.route`를 읽고 `complex`일 때만 입력에 설정된 모델을 호출합니다.

## 운영 입력

### 14B V2 단순 분석 계약 결정기

- `Fast Path 사용`: 기본 `true`
- `상세 조회 최대 행`: 기본 `5000`
- `Pivot 최대 컬럼`: 기본 `50`

Fast 판정은 질문 문자열이 아니라 조회가 끝난 payload의 실행 graph, 실제 source schema, table catalog mapping, pandas plan과 output contract를 사용합니다.

### 17 V2 Hybrid 분석 실행기

- `pandas 생성/복구 언어 모델`: Complex 경로에서만 사용
- `pandas 모델 API 키`: 기존과 같이 Global Variable 사용 가능
- `최대 Repair 횟수`: `0` 또는 `1`
- `Fast 내부 오류 시 Complex 재시도`: 기본 `false`

Fast 내부 오류의 원인을 숨기지 않도록 초기 기본값은 `false`입니다. 운영 검증 후 명시적으로 켤 수 있습니다.

### 20 V2 Hybrid 답변 생성기

- `답변 언어 모델`: Complex 경로에서만 사용
- `답변 모델 API 키`: 기존과 같이 Global Variable 사용 가능

Fast 경로에서는 실제 결과 계약과 실행 certificate로 고정 답변을 생성합니다.

## 메타데이터

별도의 V2 메타데이터 collection이나 데이터 복사는 필요하지 않습니다. 기존 입력 노드의 다음 collection 설정을 그대로 사용합니다.

- Domain: `agent_v4_domain_items`
- Table Catalog: `agent_v4_table_catalog_items`
- Main Flow Filter: `agent_v4_main_flow_filters`
- Result Store: `agent_v4_result_store`
- Session State: `agent_v4_session_states`

공정, 제품, 시간 offset, 단위, dataset key, 물리 컬럼 alias는 V2 컴포넌트에 하드코딩하지 않습니다. 선택된 Domain, Active Table Catalog, Main Flow Filter와 output contract에서만 해석합니다.

## 경로 확인

실행 payload에서 다음 항목을 확인합니다.

```json
{
  "analysis": {
    "execution_route": "fast",
    "fast_path_recipe": "ranked_summary"
  },
  "trace": {
    "inspection": {
      "fast_path": {
        "selected_route": "fast",
        "llm_calls": {
          "intent": 1,
          "pandas_generation": 0,
          "repair": 0,
          "answer": 0
        }
      }
    }
  }
}
```

의도 분석 화면에는 조회 전 계약으로 판단한 `intent_candidate`와 실제 source schema를 확인한 뒤의 `final_route`가 함께 표시됩니다.

```json
{
  "intent_plan": {
    "route_resolution": {
      "intent_candidate": "fast_candidate",
      "final_route": "fast",
      "final_recipe": "ranked_summary",
      "final_reason_codes": [
        "single_source",
        "supported_recipe",
        "schema_resolved",
        "filters_resolved"
      ]
    }
  }
}
```

Fast 경로에는 LLM 생성 pandas 코드가 없으므로 `llm_generated_code`는 빈 값입니다. 대신 `deterministic_logic_code`에 실제 dispatcher, 선택 handler와 실행 계약 인자를 표시합니다.

```json
{
  "code_generation_type": "deterministic_function",
  "deterministic_function": {
    "dispatcher": "_execute_fast_path_recipe",
    "handlers": ["_apply_fast_filters", "_fast_aggregate"],
    "recipe": "group_summary"
  }
}
```

이 코드는 LLM이 생성한 코드가 아니라 17 V2 Hybrid 분석 실행기가 실제 사용한 고정 함수 호출을 사용자 진단용으로 표현한 것입니다.

- 단일 source의 완성된 범용 계약: `fast`
- join, 다중 source, function case, 불완전한 고급 계산 계약: `complex`
- trusted catalog, 필수 조회 또는 canonical mapping 실패: `blocked`

## 생성과 검증

```powershell
python tools/build_data_analysis_flow_v2.py
python tools/build_import_ready_bundle.py
python tools/validate_flow_component_sources.py
python -m pytest tests/test_data_analysis_flow_v2.py -q
```

정확한 import 검증은 Python 3.12, Langflow 1.9.2, langflow-base 0.9.2, LFX 0.4.2 조합에서 수행합니다.
