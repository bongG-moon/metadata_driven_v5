# Langflow 1.9.2 전환 기준

## 앞으로의 기본 지침

이 문서는 일회성 전환 보고서가 아니라 현재 저장소의 기본 구현 기준이다. 별도 버전 요청이 없으면 신규 컴포넌트 구현, 기존 컴포넌트 수정, Flow JSON 재생성, import-ready bundle 생성, 테스트는 모두 아래 1.9.2 조합을 기준으로 한다.

- 개발 PC에 더 최신 Langflow가 설치되어 있어도 그 버전의 component template을 자동 채택하지 않는다.
- 다른 버전 호환이 필요하면 1.9.2 구현을 기준선으로 유지하고 별도 호환 검증을 추가한다.
- 과거 보고서에 남아 있는 1.8.2 검증 결과는 당시 이력이며 현재 기준이 아니다.
- Python 원본과 JSON을 함께 변경하고, 빌더 실행 후 두 형식의 동기화를 검증한다.

## 대상 런타임

- `langflow==1.9.2`
- `langflow-base==0.9.2`
- `lfx==0.4.2`
- Python 3.10 이상, 3.14 미만

`langflow==1.9.2`만 설치하면 설치 시점에 따라 더 최신 `langflow-base`와 `lfx`가 선택될 수 있습니다. 이 프로젝트는 Flow JSON과 기본 컴포넌트 템플릿의 재현성을 위해 세 패키지를 함께 고정합니다.

```powershell
uv venv .langflow-venv --python 3.12
uv pip install --python .langflow-venv\Scripts\python.exe `
  "langflow==1.9.2" "langflow-base==0.9.2" "lfx==0.4.2"
```

## 적용 내용

1. 10개 Flow의 `last_tested_version`과 모든 직렬화 노드 `lf_version`을 `1.9.2`로 통일했습니다.
2. Data Analysis의 기본 Language Model은 1.9.2 원본을 `tools/assets/langflow_1_9_2_language_model.py`에 고정했습니다. 더 최신 Desktop에서 재생성해도 1.10+ 전용 `provider`, `model_name` 입력이 섞이지 않습니다.
3. 빌더는 실행 중인 LFX의 `component_index.json`을 우선 사용하며, `LANGFLOW_COMPONENT_INDEX_PATH`로 검증할 인덱스를 명시할 수 있습니다.
4. `21 답변 메시지 어댑터`가 결과 테이블 미리보기 행 수의 단일 소유자가 되었습니다. Advanced 입력 `table_preview_limit`의 기본값은 10이며 MongoDB 저장 행과 다운로드 전체 데이터에는 영향을 주지 않습니다.

## 검증 결과

- 정확한 `langflow 1.9.2 / langflow-base 0.9.2 / lfx 0.4.2` 런타임 import 성공
- 10개 Flow의 node template 173/173 실제 LFX parse 성공
- 전체 pytest 419/419 성공
- 모든 export의 `last_tested_version=1.9.2`
- 모든 직렬화 노드의 `lf_version=1.9.2`

운영 인스턴스에서는 import 후 각 Flow의 모델·MongoDB Global Variable을 연결하고, Data Analysis 단일 질문과 Router 하위 Flow 호출을 각각 한 번씩 smoke test합니다.
