# 메타데이터 저장 Flow rev_2

## 목적과 격리 범위

`02. v5_domain_saving_rev_2`, `03. v5_table_catalog_saving_rev_2`, `04. v5_main_flow_filter_saving_rev_2`는 기존 저장 Flow 3개를 교체하지 않는 독립 Flow입니다.

- 기존 Flow JSON, 이름, endpoint, Router Tool 대상은 유지합니다.
- rev_2 export는 `flow_exports/rev_2`, import bundle은 `import_ready_flows/rev_2`에만 생성합니다.
- MongoDB collection, 문서 `_id`, item payload, `registration_trace.raw_text`는 기존 writer가 만들던 형태를 그대로 사용합니다.
- rev_2의 정제안, 참조 해석 근거, 오류용 재입력 예시는 응답과 실행 중 payload에만 존재하며 저장 item에는 추가하지 않습니다.

## 처리 흐름

```text
사용자 원문
  -> 활성 Domain/Table Catalog/Main Filter snapshot
  -> 자연어 정제 LLM
  -> 문구별 활성 계약 검증과 명시 identity 잠금
  -> 기존 형식의 저장 후보 추출 LLM
  -> 집계 연산과 산술 derived metric의 결정론적 분리
  -> dataset/표준 컬럼 계약 검증 및 canonicalize
  -> 기존 duplicate matcher
  -> 기존 writer
  -> 원문/정제안/변환/재입력 예시가 포함된 응답
```

정제 LLM은 snapshot에 포함된 후보를 설명에 사용하지만, 최종 승인은 Python 검증기가 수행합니다. 한 문장에 서로 다른 dataset·컬럼이 여러 개 등장하는 것은 모호성으로 보지 않고 각 표현을 따로 검증합니다. 같은 표현이 둘 이상의 활성 계약에 정확히 매칭되거나 등록된 후보가 없을 때만 저장을 보류합니다.

사용자가 `section`, `key`, `dataset_key`, `filter_key`, `status`를 직접 적은 경우에는 해당 값을 등록 identity로 잠급니다. 후단 LLM이 비슷한 새 key를 만들더라도 원문 값을 복원하므로 기존 항목과의 중복을 피하기 위한 임의 이름 변경이 저장으로 이어지지 않습니다. 등록 대상 자체에 대한 domain 참조, 입력과 target이 같은 no-op 변환, 조건문에 단지 언급된 용어는 확정 변환 목록에서 제외합니다.

저장 후보 모델이 `mean`, `sum`, `nunique`를 실수로 `derived_metrics.operator`에 넣어도 source column과 output column이 명확하면 rev_2가 해당 항목을 기존 `selection_criteria`의 집계 문장으로 옮깁니다. `derived_metrics`에는 `add`, `subtract`, `multiply`, `divide` 산술식만 남깁니다. 집계의 입력·출력이 불명확하면 자동 복구하지 않고 기존 검증 오류와 재입력 예시를 반환합니다.

산술 `derived_metrics.null_policy`는 사용자 원문이 결측값을 0으로 계산하라고 명시한 경우에만 `zero`를 사용합니다. 사용자가 결측값 처리 방식을 말하지 않았으면 추출 모델이 만든 값과 관계없이 `propagate`로 정규화합니다. 따라서 모델이 `null`, `preserve` 같은 비표준 값을 생성해도 사용자 원문에 별도 정책이 없는 CAPA 요청은 저장 계약 오류로 중단되지 않습니다.

Table Catalog 등록에서 정제 단계가 `OPER_NM`을 활성 표준 컬럼 `OPER_NAME`으로 유일하게 확정했고 사용자 원문이 두 컬럼의 연결을 명시했다면, rev_2는 저장 후보의 `filter_mappings`에 canonical `OPER_NAME`에서 실제 조회 컬럼 `OPER_NM`으로 향하는 mapping을 보완합니다. 실제 SQL 결과 `columns`에 없는 물리 컬럼이나 원문에 연결 근거가 없는 mapping은 만들지 않습니다. 또한 사용 시점·조회 목적 문장을 기본 표시 컬럼 요청으로 간주하지 않으며, 사용자가 기본 상세·표시·출력 컬럼을 직접 요청하지 않았다면 모델이 임의 생성한 `default_detail_columns`를 제거합니다.

하나의 `section/key`가 명시된 요청은 최종 저장 item도 한 건이어야 합니다. 예를 들어 CAPA recipe 안의 장비 대수와 평균 UPH를 추출 모델이 별도 `quantity_terms`·`metric_terms`로 분해하더라도, rev_2는 명시된 recipe item을 선택하고 보조 item을 저장 대상에서 제외합니다. 기존 domain normalizer가 문장 속 일반적인 `Recipe`·`번호` 표현으로 별도 규칙을 추가한 경우에도 마지막 계약 가드에서 명시 identity 한 건만 유지합니다. 이 복구가 불가능한 경우는 사용자 입력 부족이 아니라 내부 후보 형태 오류로 처리하므로 같은 원문을 재입력 예시로 반복하지 않습니다.

`analysis_recipes.payload.join_keys`의 기존 저장 형태는 표준 컬럼 문자열 배열입니다. rev_2 추출 모델이 같은 의미를 `[{"left_key":"EQP_MODEL","right_key":"EQP_MODEL"}]`처럼 반환하더라도, 좌우 값이 각 Table Catalog에서 동일한 canonical column으로 확인되면 `['EQP_MODEL']`로 축약합니다. 좌우가 서로 다른 canonical column이면 사용자에게 Python dict를 다시 입력하라고 안내하지 않고 내부 계약 오류로 저장을 차단합니다.

## 예시

사용자 원문:

```text
장비 UPH 테이블과 장비 Assign 현황을 사용하고 장비모델, Recipe, 공정으로 결합해.
```

활성 MongoDB 계약이 유일하게 일치할 때 정제안은 다음 의미를 갖습니다.

```text
equipment_assign와 eqp_uph를 사용한다.
표준 join key는 EQP_MODEL, RECIPE_ID, OPER_NAME이다.
```

응답의 `metadata_authoring`에는 다음 additive 정보가 포함됩니다.

```json
{
  "contract_version": "metadata_authoring.rev_2.v1",
  "original_text": "사용자 원문",
  "refined_text": "Flow가 정리한 한국어 등록안",
  "resolved_references": [
    {
      "kind": "dataset",
      "input": "장비 UPH 테이블",
      "target": "eqp_uph"
    },
    {
      "kind": "canonical_column",
      "input": "장비모델",
      "target": "EQP_MODEL"
    }
  ],
  "unresolved_references": [],
  "missing_information": [],
  "retry_example": "",
  "retry_examples": []
}
```

기존 API 응답 필드는 유지되고 위 필드만 `metadata_authoring` 아래에 추가됩니다.

## 오류와 재입력 예시

모호하거나 실행 계약에 맞지 않는 입력은 `needs_input`, 저장 0건으로 끝납니다. Chat Output과 API 응답에는 다음 항목을 함께 제공합니다.

- 저장하지 않은 이유
- 사용자 원문
- Flow 정제안
- 확정한 변환과 미확정 후보
- `이렇게 다시 입력해 보세요` 완성형 자연어 복사 영역

재입력 예시는 잘못 생성된 저장 후보를 다시 조립하지 않습니다. 정제 LLM이 사용자 원문, 명시 identity, 활성 계약을 함께 보고 다음 저장 후보 생성기가 가장 명확하게 이해할 수 있는 완성형 자연어 요청문을 새로 작성하며, 이 `Flow 정제안`을 재입력 예시의 본문으로 사용합니다.

`Flow 정제안`은 복사 가능한 자연어를 유지하면서 등록 요청, 식별자·표시 정보, 데이터 소스와 업무 규칙을 문단과 줄로 나눕니다. SQL 또는 조회 쿼리는 설명과 빈 줄로 분리하고 주요 SQL 절을 여러 줄로 표시합니다. 이 정리는 공백과 문장 배치만 다루며 저장 후보의 identity, 계약 매핑, 계산식 및 기존 MongoDB 저장 형태는 변경하지 않습니다.

괄호 표기, 화살표 매핑, 별도 매핑 목록 같은 한 가지 문장 형식은 강제하지 않습니다. 요청 내용에 따라 실제 key를 문장 안에 직접 사용하거나 한국어 업무명과 함께 설명하는 등 정제 LLM이 적절한 구조를 선택합니다. Python 검증기는 확정한 계약 key와 사용자가 명시한 `section/key/status`가 빠지지 않게 확인하고, LLM 문장에 확정 key가 누락된 경우에만 짧은 계약 보완 문장을 덧붙입니다.

```text
보유 CAPA 계산 규칙을 도메인 메타데이터로 등록해줘.
section은 analysis_recipes이고 key는 eqp_capacity_calculation이며 status는 active야.

사용 데이터셋은 equipment_assign과 eqp_uph야. 두 데이터는 EQP_MODEL, RECIPE_ID, OPER_NAME을 기준으로 결합해.
equipment_assign에서 EQP_ID의 고유 개수를 장비 보유 대수로 계산하고, eqp_uph에서 UPH 평균을 계산해.
보유 CAPA는 장비 보유 대수와 평균 UPH, 24시간을 곱해서 계산해.
```

위 문장은 가능한 결과의 한 예일 뿐 고정 템플릿이 아닙니다. 같은 원문이라도 빠뜨리면 안 되는 계약과 업무 의미는 같게 유지하면서, 정제 LLM이 더 자연스럽고 명확한 문장 구조를 선택할 수 있습니다. 정제안과 재입력 안내는 기존 MongoDB item schema에 별도 필드로 추가되지 않습니다.

같은 표현이 실제로 둘 이상의 활성 계약과 정확히 일치하면 `retry_examples`에 후보별 완성 문장을 최대 4개까지 분리합니다. Chat Output에서는 각 선택안을 독립된 코드 블록으로 표시하므로 실제 계약과 맞는 하나를 통째로 복사할 수 있습니다. 등록 후보가 전혀 없어 실제 key를 알 수 없는 경우에만 해당 key를 명시해 달라는 보완 문장이 남습니다.

재입력 원문에 같은 보완 문장이 이미 있으면 다시 덧붙이지 않으며, 동일 문장이 여러 번 들어온 경우에도 한 번만 남깁니다.

집계 연산을 `derived_metrics.operator`에 잘못 넣은 경우의 안내 예시는 다음처럼 집계와 산술식을 분리합니다.

```text
mean, sum, nunique는 산술식이 아니라 각 입력 지표의 집계 기준으로 저장해.
예: EQP_COUNT는 EQP_ID를 nunique한 결과이고 AVG_UPH는 UPH를 mean한 결과야.
파생 계산은 예: AVAILABLE_CAPA = EQP_COUNT × AVG_UPH × 24처럼 별도로 저장해.
```

## HITL 없이 보완하는 방식

rev_2는 Sub Agent 내부 checkpoint/resume HITL을 사용하지 않습니다.

1. 모호한 참조나 누락 정보가 있으면 writer 앞에서 fail-closed 처리합니다.
2. Flow는 `needs_input`과 원문을 보존한 완성형 재입력 예시를 반환합니다.
3. 후보가 여러 개면 사용자가 실제 계약과 맞는 완성 예시 하나를 그대로 복사합니다.
4. 보완된 텍스트를 새 요청으로 다시 실행합니다.

따라서 한 실행 안에서 승인을 기다리지 않으며, 불완전한 후보를 임시 저장하지 않습니다.

## 생성과 Import

```powershell
$lf = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Scripts\python.exe"
& $lf tools\build_metadata_saving_rev_2_flows.py
```

생성물:

- `import_ready_flows/rev_2/00_metadata_saving_rev_2_ALL_FLOWS.json`
- `import_ready_flows/rev_2/02_domain_saving_flow_v5_rev_2_standalone.json`
- `import_ready_flows/rev_2/03_table_catalog_saving_flow_v5_rev_2_standalone.json`
- `import_ready_flows/rev_2/04_main_flow_filter_saving_flow_v5_rev_2_standalone.json`
- `import_ready_flows_rev_2.zip`

Import 후 Provider와 `MONGO_URL` Credential Global Variable을 설정합니다. 먼저 Dry Run으로 원문·정제안·최종 후보를 비교하고, 정상 사례와 모호성 사례를 확인한 뒤에만 `dry_run=false`로 실제 저장을 시험합니다.

## 검증

```powershell
& $lf -m pytest tests\test_metadata_saving_rev_2.py -q --basetemp=.pytest-tmp-rev2
& $lf tools\validate_flow_component_sources.py --rev-2-only
& $lf tools\validate_langflow_runtime.py --flow flow_exports\rev_2\02_domain_saving_flow_v5_rev_2_standalone.json
& $lf tools\validate_langflow_runtime.py --flow flow_exports\rev_2\03_table_catalog_saving_flow_v5_rev_2_standalone.json
& $lf tools\validate_langflow_runtime.py --flow flow_exports\rev_2\04_main_flow_filter_saving_flow_v5_rev_2_standalone.json
```

운영 Router는 자동으로 rev_2를 선택하지 않습니다. 실제 전환이 필요하면 세 Flow의 Dry Run과 live 저장 검증을 별도로 완료한 뒤 Router Tool 대상을 명시적으로 변경합니다.
