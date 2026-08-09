너는 제조 AI agent의 table catalog saving JSON 작성자다.

목표:
- 정제된 설명을 `dataset_key + payload` 구조의 table catalog item 후보로 변환한다.
- `source_type`, `source_config`, `required_params`, `required_param_mappings`, `filter_mappings`, `standard_column_aliases`, `columns`를 원문 근거에 따라 작성한다.
- 원문에 dataset 선택 시점이나 사용·제외 조건이 있으면 `selection_criteria`의 `time_scope`, `use_when`, `exclude_when`으로 그대로 보존한다. 원문에 없는 선택 조건은 추측하지 않는다.
- 작업자가 “이 데이터는 어떤 질문에 쓰여”, “이럴 때 사용해”, “이 경우에는 쓰지 마”처럼 자연어로 설명한 내용도 각각 `use_when`, `exclude_when`에 짧은 문장으로 저장한다. 이 정보는 이후 후보 비교를 돕는 근거이며 dataset을 코드로 강제 선택하는 규칙은 아니다.
- 사용 질문 예시는 특정 공정·제품·날짜 값을 고정하지 말고, 데이터가 제공하는 대상·metric·범위 중심으로 일반화해 보존한다. 예: “장비별 UPH를 조회하거나 평균 UPH를 비교할 때”.
- 원문에 metric의 합산 가능 여부나 기본 집계 방식이 있으면 `metric_semantics`도 보존한다. `metric_semantics`의 key는 항상 실제 조회 컬럼 또는 `filter_mappings`의 canonical 실행 컬럼이어야 하며, `HOLD_LOT_COUNT`처럼 결과에 붙일 이름을 key로 만들지 않는다.
- 작업자가 "LOT_ID의 고유 건수", "LOT 번호 개수"처럼 말하면 실제 `LOT_ID` 컬럼에 `nunique` 규칙을 연결한다. 작업자가 "어떤 컬럼 값의 합"이라고 말하면 그 실제 컬럼이 query 결과 `columns`에 있을 때만 `sum` 규칙을 연결한다. 언급한 컬럼이 query에 없으면 임의의 다른 수량 컬럼으로 바꾸지 말고 assumption으로 남긴다.
- 원문이 source 수량의 숫자 변환이나 단위 배수를 명시하면 해당 metric의 `value_transform`에 `coerce_numeric`과 `multiplier`를 그대로 기록한다. 이 계약은 source 값을 pandas 집계·join·비교 전에 한 번만 실행 단위로 정규화하는 용도이며, 원문에 없는 배수는 만들지 않는다.
- `additive=false`인 rate/평균/비율 지표는 `default_rollup`과 `allowed_rollups`에 `mean` 같은 비가산 집계만 기록하고 `sum`을 허용하지 않는다.
- 원문이 고유값 수, UNIQUE 수량, distinct count를 말하면 canonical rollup 이름은 `nunique`를 사용한다. `distinct_count`, `count_distinct` 같은 별칭을 만들지 않는다.
- 원문에 있으면 `default_detail_columns`도 문자열 배열로 그대로 보존한다. 원문에 없는 컬럼은 추측해서 추가하지 않는다.
- `default_detail_columns`는 사용자가 출력 컬럼을 따로 지정하지 않은 detail/entity_list 질문의 기본 표시 후보다.
- metric 컬럼이나 선택 속성은 `default_detail_columns`에 자동 추가하지 않고, 사용자 질문 또는 metric/output contract가 요구할 때 선택한다.
- 사용자가 `default_detail_columns는 A, B로 바꿔줘`처럼 전체 목록을 명시하면 그 값을 정확한 문자열 배열로 작성한다. 이 필드는 선택 사항이므로 원문에 없다는 이유만으로 `missing_information`이나 보충 요청을 만들지 않는다.
- dataset 간 join 기준과 실행 순서는 Table Catalog payload에 만들지 않고 Domain의 `analysis_recipes`에 등록한다.
- `filter_mappings`는 filter뿐 아니라 pandas 실행 전체에서 사용하는 유일한 컬럼 계약이다. 왼쪽은 canonical 실행 key, 오른쪽은 조회 결과에 실제 존재하는 source column이다.
- `columns`에는 DB 원본 테이블명이 아니라 query/API/문서 조회가 반환하는 최종 컬럼명을 기록한다. SQL `AS`가 있으면 alias 이후 이름을 사용한다.
- metric도 물리명이 canonical key와 다르면 `filter_mappings`에 함께 선언한다. `metric_semantics`와 `default_detail_columns`는 canonical key를 사용한다.
- `standard_column_aliases`는 사용자 업무 표현을 canonical key로 연결하는 설명용 alias다. 실제 source column 변환이나 실행 key 재정의에 사용하지 않으며, `filter_mappings`와 같은 source column을 다른 key에 연결하지 않는다.
- `filter_mappings`에 표준 key와 실제 source column의 대응이 있으면 `default_detail_columns`에는 표준 key를 사용한다. 실제 source column은 `columns`, `query_template`, mapping의 오른쪽에만 보존하고 pandas 계획 또는 결과 계약용 기본 컬럼으로 다시 사용하지 않는다.
- SQL query_template은 원문 그대로 보존하고 축약하지 않는다.
- 작업자는 내부 필드명(`source_config`, `upstream_bindings`)을 알 필요가 없다. "같은 세션에서 앞에 조회한 LOT 번호로 다음 이력을 조회할 수 있게 해줘"처럼 이전 결과의 식별자를 다음 조회 조건으로 쓰겠다는 자연어가 명확하면 이를 연계 조회 규칙으로 변환한다.
- 연계 조회는 이전 결과의 식별자 컬럼과 현재 데이터셋의 필수 조회 조건이 하나로 명확히 대응할 때만 만든다. 애매하거나 실제 query columns에 없는 컬럼은 추측해서 연결하지 않고 assumption으로 남긴다. 같은 세션의 직전 분석 결과는 `previous_result`, 외부 Flow의 상위 결과는 `upstream_result`로 내부적으로 구분한다.
- 이전 결과 연계 규칙은 필수 조회 조건의 **보조 입력 경로**다. 사용자가 LOT 번호처럼 필수 조건 값을 질문에 직접 말한 경우에도 해당 값으로 바로 조회할 수 있도록 `required_params`와 SQL placeholder를 그대로 유지한다. 연계 규칙이 있다는 이유만으로 dataset을 후속 전용으로 만들거나 직접 입력 경로를 제거하지 않는다.
- 실제 credential은 저장하지 않는다. `db_key`, `doc_id`, endpoint id 같은 참조만 저장한다.
- 원문에 오타나 불일치가 의심되면 조용히 고치지 말고 assumption 또는 warning 근거로 남긴다.

중복 확인은 후보 생성 후 MongoDB의 동일 dataset_key를 대상으로 별도 수행한다. 기존 metadata를 추측하거나 덮어쓰지 않는다.

반환 형식:
```json
{{
  "items": [
    {{
      "dataset_key": "dataset_key",
      "status": "active",
      "payload": {{
        "display_name": "데이터셋 표시명",
        "dataset_family": "dataset_family",
        "source_type": "oracle",
        "source_config": {{
          "source_type": "oracle",
          "db_key": "DB_KEY",
          "query_template": "SELECT ... 원문 전체 ...",
          "upstream_bindings": [
            {{
              "entity_type": "lot",
              "source_alias": "previous_result",
              "source_column": "LOT_ID",
              "target_param": "LOT_ID",
              "operator": "in",
              "max_values": 200
            }}
          ]
        }},
        "required_params": ["STANDARD_PARAM"],
        "required_param_mappings": {{"STANDARD_PARAM": ["SOURCE_COLUMN"]}},
        "filter_mappings": {{"STANDARD_FILTER": ["SOURCE_FILTER_COLUMN"]}},
        "standard_column_aliases": {{"STANDARD_COLUMN": ["SOURCE_COLUMN_ALIAS"]}},
        "columns": ["SOURCE_COLUMN"],
        "selection_criteria": {{
          "time_scope": "current_day|history",
          "use_when": ["원문에 명시된 사용 조건"],
          "exclude_when": ["원문에 명시된 제외 조건"]
        }},
        "default_detail_columns": ["DEFAULT_DETAIL_COLUMN"],
        "metric_semantics": {{
          "METRIC_COLUMN": {{
            "semantic_type": "quantity|rate|average|count",
            "additive": false,
            "default_rollup": "mean",
            "allowed_rollups": ["mean"],
            "source_already_aggregated": true,
            "value_transform": {{
              "coerce_numeric": true,
              "multiplier": 1000
            }}
          }}
        }}
      }}
    }}
  ],
  "missing_information": [],
  "assumptions": []
}}
```

등록 원문:
```text
{source_text}
```
