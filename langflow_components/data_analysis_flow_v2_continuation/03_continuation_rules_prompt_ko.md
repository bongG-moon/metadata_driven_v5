종속 조회 규칙:

- 1차 결과 식별자가 2차 dataset의 빈 필수 파라미터를 채워야 할 때만 `dependent_retrieval_plan`을 작성한다. 일반 단일 단계와 독립 다중-source join에는 작성하지 않는다.
- stage는 정확히 2개다. stage2는 stage1만 `depends_on`하며, 각 stage에 완전한 `retrieval_jobs`, `pandas_execution_plan`, `output_contract`를 둔다.
- `handoff.columns`와 stage1 결과 컬럼, stage2 `input_bindings`의 required parameter·upstream binding은 선택된 Catalog에 명시된 값만 사용한다. dataset·parameter·column을 추측하지 않는다.
- 별도 이력의 최신 상세는 stage2에서 `select_extreme_row_per_group`으로 선택한다. `partition_by`, `order_by`, `limit_per_group=1`, `tie_policy`, `tie_breakers`, `projection`, `strict=true`를 명시해 시각·코드·설명이 같은 행에서 나오게 한다.
- Catalog tie-breaker가 있으면 `tie_policy=first`와 해당 컬럼을 사용한다. 없으면 빈 `tie_breakers`와 `tie_policy=error`로 동률을 차단한다. `include_all`은 primary 정렬 경계 동률 행을 모두 유지한다.
- 최종 결합은 `upstream_result`를 보존하는 left join이다. 빈 1차 결과나 불완전 binding을 임의 조회로 대체하지 않는다.
