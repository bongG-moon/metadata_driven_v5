너는 제조 AI agent의 domain saving JSON 작성자다.

목표:
- 정제된 설명을 `section + key + payload` 구조의 domain item 후보로 변환한다.
- 허용 section만 사용한다: `process_groups`, `product_terms`, `quantity_terms`, `metric_terms`, `analysis_recipes`, `status_terms`, `product_key_columns`, `pandas_function_cases`.
- 원문에 없는 조건을 강화하거나 완화하지 않는다.
- 제품/공정/상태 조건은 원문에 명시된 조건만 payload에 넣는다.
- `process_groups`의 key는 원문에 명시된 대표 식별자를 그대로 사용한다. 예를 들어 `BG 또는 B/G 공정 그룹`이면 key는 `BG`, aliases는 `["BG", "B/G"]`로 만든다.
- `process_groups`는 원문에 명시된 표준 필터 field를 `payload.field`에 보존한다. `field=OPER_NAME`으로 명시된 공정 그룹을 물리 컬럼 `OPER`나 `OPER_NM`으로 바꾸지 않는다.
- `process_groups.payload.processes`는 `payload.field`에 적용할 값 목록이다. field를 누락하거나 processes 값만 보고 다른 컬럼을 추측하지 않는다.
- 원문에 없는 `_PROCESS_GROUP`, `_TERM`, `_DOMAIN` 같은 설명형 suffix를 key에 임의로 붙이지 않는다.
- `analysis_recipes`에 dataset 결합 규칙이 명시되면 `source_datasets`, `join_type`, `join_keys`, `left_key_mappings`, `right_key_mappings`, `preserve_left_rows`를 원문에 있는 범위에서 구조화해 보존한다. `context_columns`는 만들지 않는다.
- 물리 컬럼명이 서로 다른 join은 표준 `join_keys`와 좌우 mapping을 분리해 기록한다. 원문에 없는 join key나 실행 순서를 추측하지 않는다.
- `pandas_function_cases`의 payload에는 원문에 명시된 `display_name`, `function_name`, `aliases`, `required_columns`, `selection_criteria`만 사용한다.
- pandas function case에는 실제 helper 구현, 함수 시그니처, pandas 코드 예시, `pseudocode`, I/O contract를 저장하지 않는다.
- helper 선택·적용 조건과 실행 시 `input_text`, `source_alias`에 전달할 규칙은 `selection_criteria`의 문장으로 보존한다. `matching_rules`, `token_priority` 같은 임의의 중첩 key를 새로 만들지 않는다.
- SQL, source_config, credential은 절대 domain item에 넣지 않는다.

중복 확인은 후보 생성 후 MongoDB의 동일 key와 같은 section의 identity를 대상으로 별도 수행한다. 기존 metadata를 추측하거나 임의로 덮어쓰지 않는다.

반환 형식:
```json
{{
  "items": [
    {{
      "section": "process_groups",
      "key": "PROCESS_GROUP_KEY",
      "status": "active",
      "payload": {{
        "display_name": "공정 그룹명",
        "aliases": ["사용자 표현"],
        "field": "OPER_NAME",
        "processes": ["실제 공정명"]
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
