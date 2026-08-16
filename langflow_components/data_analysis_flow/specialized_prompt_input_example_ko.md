선택된 Domain과 Table Catalog를 실행 의미의 단일 기준으로 사용하는 현장 특화 지침이다.

- 이 텍스트에는 dataset key, 물리 컬럼명, 공정명, 제품명, 날짜 offset, metric 값, filter 값을 하드코딩하지 않는다. 실제 값은 `metadata_candidates`에서 선택한 문서의 payload를 사용한다.
- 후보 Domain의 `section`, `key`, aliases, condition/conditions, selection_criteria, temporal_semantics, metric_semantics, execution_contract를 질문 의미와 대조해 필요한 문서만 `metadata_refs`에 남긴다.
- 선택된 Domain/Recipe의 시간 계약만 사용한다. dataset family, business timepoint, query date offset, disallowed dataset은 다른 규칙이나 이전 질문에서 추측하지 않는다.
- 선택된 `process_groups`의 canonical field와 등록 process 목록을 공정 filter에 사용한다. 세부 공정 annotation이나 질문 매칭 결과가 있으면 그 범위만 사용하고, 등록되지 않은 공정명·그룹을 만들지 않는다.
- 선택된 `product_terms`, `status_terms`, `quantity_terms`의 조건과 family별 조건을 해당 source에만 적용한다. 서로 다른 metric/source의 조건을 합치지 않는다.
- 제품 grain, entity grain, join, 기본 상세 컬럼은 선택된 `product_key_columns`와 `analysis_recipes`, Table Catalog의 계약으로 결정한다. 공통 프롬프트에 없는 별도 키나 join type을 추측하지 않는다.
- 선택된 `pandas_function_cases` 중 실행 가능하고 intent 선택이 허용된 helper만 사용한다. helper 선택 시 metadata의 입력·순서·중복 filter 계약을 그대로 따른다.
- 질문이 제품명·제품 코드·제품 속성으로 대상을 특정하고 선택 가능한 `product_token_match`가 있으면 반드시 선택한다. 특히 하이픈을 포함한 영문/숫자 제품 식별 토큰도 임의의 단일 조회 컬럼 값이 아니다.
- 이때 제품 표현과 식별 토큰 전체는 `match_product_tokens.input_text`로만 전달한다. 제품 토큰으로 `eq`·`in`·`starts_with` 조회 filter를 만들지 않으며, 날짜·공정·지표처럼 제품 외 조건만 조회 filter로 남긴다.
- 특화 metadata가 같은 표현에 대해 충돌하거나 실행 컬럼·metric·필수 파라미터를 유일하게 확정하지 못하면 임의의 fallback을 만들지 말고 clarification을 반환한다.
- 특화 지침은 공통 실행 계약을 대체하지 않는다. canonical column, typed input, output contract, null 정책, 중복 의미 컬럼 금지 규칙은 공통 계약을 그대로 따른다.
