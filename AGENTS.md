# metadata_driven_v5 작업 지침

이 지침은 저장소 전체에 적용한다.

## 기본 Langflow 기준

- 별도 요청이 없으면 모든 신규 구현, 수정, Flow JSON 재생성, import 검증은 `Langflow 1.11.0`을 기준으로 수행한다.
- 재현 가능한 정확한 패키지 조합은 다음과 같다.
  - `langflow==1.11.0`
  - `langflow-base==0.11.0`
  - `lfx==1.11.0`
- 기본 검증 인터프리터는 Langflow Desktop 1.11이 관리하는 Python 3.13이다. 별도 가상환경을 만들 때도 검증 결과에는 실제 Python·패키지 버전을 함께 남긴다.
- `latest` 또는 설치 환경에 우연히 존재하는 다른 minor 버전의 Langflow/LFX 템플릿을 기본값으로 사용하지 않는다.
- 1.9.2 Flow는 `langflow_1.9.0` 브랜치의 레거시 기준이며, 현재 브랜치의 구현·검증 기준으로 사용하지 않는다.

## 구현·JSON 동기화 원칙

- Python custom component와 Flow JSON을 함께 수정하고, 원본과 export/import-ready JSON이 항상 동기화되도록 한다.
- 빌더는 1.11.0의 기본 컴포넌트 원본과 schema를 사용해야 한다. 기본 Language Model은 `tools/assets/langflow_1_11_0_language_model.py`를 사용하며, 1.11의 `model_name`·`provider` override 입력을 제거하지 않는다.
- 기본 컴포넌트 인덱스는 1.11.0 `lfx/_assets/component_index.json`을 사용한다. 필요하면 `LANGFLOW_COMPONENT_INDEX_PATH`로 정확한 1.11.0 인덱스를 지정한다.
- 빌더가 생성한 Flow와 import-ready bundle의 `last_tested_version`, 각 node의 `lf_version`은 `1.11.0`이어야 한다.
- 1.11 `Message`의 `data`에는 표시 `text`가 함께 직렬화된다. Message에 trace/metadata를 붙일 때 `message.data` 전체를 대입하지 말고 기존 mapping에 키를 추가한다.
- 기본 Agent와 Tool의 새 기능(HITL 승인, A2A, AG-UI)은 기존 읽기 전용 분석 경로에 자동으로 섞지 않는다. 새 경로로 채택할 때는 별도 Flow 계약과 실행 검증을 추가한다.

## Standalone 원칙

- 모든 custom component는 Langflow standalone 환경에서 단독으로 동작해야 한다.
- 실행에 필요한 MongoDB URI, database, collection, timeout, 표시 행 수 같은 운영 설정은 환경변수에만 숨기지 않고 해당 노드 입력에서 확인하고 조정할 수 있어야 한다.
- 연결된 다른 Python 파일을 런타임에 import해야만 동작하는 구조는 사용하지 않는다. 공통 업무 규칙은 빌더가 standalone component source에 포함하도록 유지한다.

## 변경 후 검증

- 변경 범위에 맞는 pytest와 대표 질문 검증을 실행한다.
- `tools/validate_flow_component_sources.py`로 Python 원본과 export/import JSON 동기화를 확인한다.
- 1.11.0 정확한 런타임 조합에서 모든 node template이 parse되는지 확인한다. LFX upgrade 검사는 기본 native node의 보류 업그레이드를 실패로 처리하고, standalone custom component의 별도 검증은 source/template parse로 수행한다.
- 생성된 9개 Flow의 `last_tested_version=1.11.0`와 모든 node의 `lf_version=1.11.0`를 확인한다.
- 과거 문서의 1.8.2 수치는 당시 검증 이력일 뿐 현재 구현 기준으로 사용하지 않는다.

상세 설치 및 전환 기준은 `docs/LANGFLOW_1_11_MIGRATION.md`를 따른다.
