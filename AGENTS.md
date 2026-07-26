# metadata_driven_v5 작업 지침

이 지침은 저장소 전체에 적용한다.

## 기본 Langflow 기준

- 별도 요청이 없으면 모든 신규 구현, 수정, Flow JSON 재생성, import 검증은 `Langflow 1.9.2`를 기준으로 수행한다.
- 재현 가능한 정확한 패키지 조합은 다음과 같다.
  - `langflow==1.9.2`
  - `langflow-base==0.9.2`
  - `lfx==0.4.2`
- Python은 3.10 이상 3.14 미만을 사용하며, 기본 검증 버전은 Python 3.12로 한다.
- `latest` 또는 설치 환경에 우연히 존재하는 더 최신 Langflow/LFX 템플릿을 기본값으로 사용하지 않는다.
- 다른 Langflow 버전 호환 작업은 사용자가 명시적으로 요청한 경우에만 수행하고, 1.9.2 기준 동작을 깨뜨리지 않는지 별도로 검증한다.

## 구현·JSON 동기화 원칙

- Python custom component와 Flow JSON을 함께 수정하고, 원본과 export/import-ready JSON이 항상 동기화되도록 한다.
- 더 최신 Langflow 환경에서 빌더를 실행할 때도 1.9.2 기본 컴포넌트 원본과 schema가 유지되어야 한다.
- 기본 Language Model은 `tools/assets/langflow_1_9_2_language_model.py`를 사용한다.
- 기본 컴포넌트 인덱스는 1.9.2의 `lfx/_assets/component_index.json`을 사용한다. 필요하면 `LANGFLOW_COMPONENT_INDEX_PATH`로 정확한 1.9.2 인덱스를 지정한다.
- 빌더가 생성한 Flow와 import-ready bundle의 `last_tested_version`, 각 node의 `lf_version`은 `1.9.2`여야 한다.

## Standalone 원칙

- 모든 custom component는 Langflow standalone 환경에서 단독으로 동작해야 한다.
- 실행에 필요한 MongoDB URI, database, collection, timeout, 표시 행 수 같은 운영 설정은 환경변수에만 숨기지 않고 해당 노드 입력에서 확인하고 조정할 수 있어야 한다.
- 연결된 다른 Python 파일을 런타임에 import해야만 동작하는 구조는 사용하지 않는다. 공통 업무 규칙은 빌더가 standalone component source에 포함하도록 유지한다.

## 변경 후 검증

- 변경 범위에 맞는 pytest와 대표 질문 검증을 실행한다.
- `tools/validate_flow_component_sources.py`로 Python 원본과 export/import JSON 동기화를 확인한다.
- 1.9.2 정확한 런타임 조합에서 모든 node template이 parse되는지 확인한다.
- 생성된 10개 Flow의 `last_tested_version=1.9.2`와 모든 node의 `lf_version=1.9.2`를 확인한다.
- 과거 문서의 1.8.2 수치는 당시 검증 이력일 뿐 현재 구현 기준으로 사용하지 않는다.

상세 설치 및 전환 기준은 `docs/LANGFLOW_1_9_2_MIGRATION.md`를 따른다.
