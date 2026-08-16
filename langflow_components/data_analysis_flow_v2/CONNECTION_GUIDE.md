# Data Analysis Flow V2 연결 가이드

## 기본 경로

Data Analysis V2가 기본 `data_analysis` 경로입니다. 외부 계약은 기존과 같습니다.

- Flow 표시명: `01. v5_data_analysis`
- endpoint: `metadata-driven-v5-data-analysis`
- Tool 이름: `run_data_analysis`
- import 파일: `01_data_analysis_flow_v2_standalone.json`

현재 import-ready 묶음에는 V1 legacy Flow를 포함하지 않습니다. 기존 Router가 이전 Flow ID를 저장하고 있다면 import 후 `01. v5_data_analysis`를 다시 선택해야 합니다. 런타임 ID는 코드에서 임의로 초기화하지 않습니다.

## 실행 흐름

```text
Intent LLM
  -> 조회 계약 정규화/검증
  -> 데이터 조회 및 canonical 컬럼 표준화
  -> 14B 경로/실행 모드 확정
      -> Fast: 결정론적 Fast recipe
      -> Complex + deterministic_contract: 결정론적 병합/비교 계약
      -> Complex + llm_pandas: pandas LLM 및 오류 시 repair 1회
  -> 결과 저장
  -> 20 Hybrid Answer Builder
      -> Fast: 결정론적 답변
      -> Complex + 답변 LLM OFF: 결정론적 답변
      -> Complex + 답변 LLM ON: Bool 분기 후 AnswerEvidence 생성 및 LLM 호출
  -> 세션 저장 및 runtime payload 정리
```

14B는 질문 문자열을 하드코딩하지 않습니다. 정규화된 Typed IR, 조회 source 수, 실제 canonical schema, 필터, grain, metric, join 및 출력 계약으로 경로를 결정합니다.

## 경로와 모델 호출

| 경로 | Intent | pandas 생성 | Repair | Answer |
|---|---:|---:|---:|---:|
| Fast | 1 | 0 | 0 | 0 |
| Blocked | 1 | 0 | 0 | 0 |
| Complex deterministic | 1 | 0 | 0 | ON일 때만 1 |
| Complex LLM, Answer OFF | 1 | 1 | 실패 시 최대 1 | 0 |
| Complex LLM, Answer ON | 1 | 1 | 실패 시 최대 1 | 1 |

`analysis.execution_route`는 계속 `fast`, `complex`, `blocked` 중 하나입니다. Complex 내부 방식은 다음 필드로 확인합니다.

```json
{
  "analysis": {
    "execution_route": "complex",
    "analysis_execution_mode": "deterministic_contract",
    "execution_mode": "merge_metric_sources"
  },
  "trace": {
    "inspection": {
      "fast_path": {
        "requires_pandas_llm": false,
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

## LLM 입력 축소 원칙

- 실행용 `runtime_sources`, 전체 결과, 다운로드 및 후속 질문용 reference는 저장 완료 전까지 유지합니다.
- pandas LLM에는 관련 canonical 컬럼을 우선한 최대 2행, 16컬럼, 셀 160자의 model view만 전달합니다.
- Answer LLM에는 최대 5행, 16컬럼, 셀 160자의 `AnswerEvidence`만 전달합니다.
- `Complex 답변 LLM 사용`이 꺼져 있으면 Answer prompt도 생성하지 않습니다.
- repair prompt에는 실패 코드를 한 번만 전달합니다.
- 축소는 LLM view에만 적용하며 결과 테이블, CSV 다운로드, MongoDB Result Store 및 후속 질문 state 계약은 변경하지 않습니다.

## 운영 입력

### 14B Simple Analysis Contract Resolver

- `Fast Path 사용`: 기본 `true`
- `상세 조회 최대 행`: 기본 `5000`
- `Pivot 최대 컬럼`: 기본 `50`

### 17 Hybrid Analysis Executor

- pandas 모델: `llm_pandas` 모드에서만 사용
- 최대 Repair 횟수: `0` 또는 `1`
- Fast 내부 오류 시 Complex fallback: 기본 `false`

### 20 Hybrid Answer Builder

- `Complex 답변 LLM 사용`: BoolInput, 기본 `true`
- `false`이면 Complex도 결정론적 답변 사용
- `true`이면 결과 저장 이후 제한된 AnswerEvidence로만 답변 모델 호출

## 메타데이터

V2 전용 metadata 복사본은 필요하지 않습니다. 기존 Domain, Table Catalog, Main Flow Filter, Result Store 및 Session State 설정을 사용합니다. dataset, 공정, 제품, 시간, 단위 및 물리 컬럼 alias는 컴포넌트에 질문별로 하드코딩하지 않습니다.

## 생성과 검증

```powershell
python tools/build_data_analysis_flow_v2.py
python tools/build_import_ready_bundle.py
python tools/validate_data_analysis_v2_routes.py
python tools/validate_flow_component_sources.py
python -m pytest tests/test_data_analysis_flow_v2.py tests/test_v5_flow_export.py -q
```

정확한 import/runtime 검증은 Langflow Desktop Python 3.13, Langflow 1.11.0, `langflow-base==0.11.0`, `lfx==1.11.0` 조합을 기준으로 합니다.
