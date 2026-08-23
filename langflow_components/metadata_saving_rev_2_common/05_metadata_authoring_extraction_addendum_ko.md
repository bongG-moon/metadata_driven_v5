[rev_2 단일 등록 identity 규칙]

- 정제된 설명에 section과 key, dataset_key 또는 filter_key가 하나 명시되어 있으면 items는 정확히 1건만 반환한다.
- 명시된 section, key, dataset_key, filter_key, status를 그대로 사용한다. 비슷한 이름으로 바꾸거나 보조 item을 새로 만들지 않는다.
- 하나의 analysis_recipes 등록 요청에 등장한 입력 집계값과 최종 계산값은 모두 그 analysis_recipes item의 payload 안에 표현한다.
- 장비 대수, 평균 UPH 같은 중간 지표를 별도 quantity_terms 또는 metric_terms item으로 분리하지 않는다.
- mean, sum, nunique 같은 집계 기준은 selection_criteria에 보존하고, derived_metrics에는 add, subtract, multiply, divide 산술 계산만 넣는다.
- derived_metrics의 null_policy는 zero 또는 propagate만 사용한다. 정제된 설명에 결측값을 0으로 계산한다는 명시가 없으면 propagate를 사용한다.
- Table Catalog에서 표준 컬럼과 조회 결과의 실제 컬럼 관계가 명시되면 filter_mappings의 왼쪽에는 표준 컬럼, 오른쪽에는 실제 조회 컬럼을 넣는다. 예를 들어 공정의 표준 컬럼이 OPER_NAME이고 SQL 결과 컬럼이 OPER_NM이면 OPER_NAME을 key로, OPER_NM을 값으로 기록한다.
- 사용 시점이나 조회 목적 설명을 default_detail_columns로 바꾸지 않는다. default_detail_columns는 사용자가 기본 표시·상세·출력 컬럼을 직접 요청한 경우에만 만든다.
- analysis_recipes의 join_keys는 `["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]`처럼 표준 컬럼 문자열 배열로 반환한다. `[{{"left_key": "EQP_MODEL", "right_key": "EQP_MODEL"}}]` 같은 object 배열을 만들지 않는다.
- 좌우 물리 컬럼이 다르더라도 join_keys에는 공통 표준 컬럼만 넣고, 원문에 실제 좌우 mapping이 명시된 경우에만 기존 left_key_mappings와 right_key_mappings 필드를 별도로 사용한다.
- 정제된 설명이 REV_2 저장 보류 상태라면 items는 빈 배열로 반환한다.
