# Route Flow V3 Tool 설명 작성 가이드

Route V3는 Tool 이름만으로 분류하지 않고, 각 Tool이 어떤 입력을 받고 어떤 결과 참조를 만들 수 있는지를 설명에서 판단한다. 각 `01 연계 실행용 이름 기반 Cached Run Flow 도구`의 `tool_description`에는 아래 네 항목을 짧게 포함한다.

1. 수행하는 업무
2. 호출해야 하는 질문 유형
3. `upstream_result_ref` 지원 여부와 참조가 의미하는 대상
4. 후속 Tool에 전달할 `result_ref` 생성 여부

## 권장 예시

### Data Analysis

```text
제조 데이터의 조회, 집계, 계산, 비교와 pandas 분석을 수행한다. 독립 질문에는 question만 전달한다. 이전 분석 결과를 대상으로 추가 조회할 때는 upstream_result_ref를 지원하며, 성공 시 후속 분석용 result_ref를 반환한다.
```

권장 설정:

- `accepts_upstream_result_ref=true`
- `can_produce_result_ref=true`
- `entity_id_columns=LOT_ID, EQP_ID`
- `return_direct=false`

### Metadata QA

```text
등록된 데이터셋, 도메인, 컬럼, 필수 파라미터와 계산 규칙을 설명한다. 분석 결과를 입력으로 받지 않으며 후속 데이터 분석용 result_ref를 만들지 않는다.
```

권장 설정:

- `accepts_upstream_result_ref=false`
- `can_produce_result_ref=false`
- `entity_id_columns` 비움
- `return_direct=false`

### 이상 LOT 조회

```text
지정 기간과 조건에서 이상 LOT을 분석한다. 독립 실행용이며 성공 시 LOT 전체 결과를 가리키는 result_ref와 LOT_ID preview를 반환해 HOLD 이력 등 후속 분석에 사용할 수 있다.
```

권장 설정:

- `accepts_upstream_result_ref=false`
- `can_produce_result_ref=true`
- `entity_id_columns=LOT_ID`
- `return_direct=false`

### Metadata 저장

```text
사용자가 명시적으로 요청한 도메인 메타데이터 등록 또는 변경을 수행한다. 조회나 분석 결과를 자동 저장하지 않으며 한 요청에서 한 번만 호출한다.
```

권장 설정:

- `accepts_upstream_result_ref=false`
- `can_produce_result_ref=false`
- `entity_id_columns` 비움
- `return_direct=false`

`upstream_result_ref`를 지원하지 않는 Tool에도 외부 schema의 선택 필드는 보이지만 값을 넣으면 명시적 오류가 발생한다. Agent가 잘못 전달하지 않도록 Tool 설명과 시스템 프롬프트 양쪽에 지원 여부를 유지한다.
