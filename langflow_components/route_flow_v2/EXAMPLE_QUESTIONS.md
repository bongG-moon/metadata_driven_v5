# Route Flow v2 예시 질문 (07 Agent + Tool)

| 질문 | 기대 동작 |
| --- | --- |
| `오늘 DA 공정 생산량 알려줘` | `run_data_analysis` 1회 |
| `현재 D/A1 공정에 배정된 장비와 해당 모델의 UPH를 보여줘` | `run_data_analysis` 1회 |
| `target 데이터셋의 필수 파라미터와 컬럼을 설명해줘` | `run_metadata_qa` 1회 |
| `BG 또는 B/G 그룹에 B/G1부터 B/G5까지 포함하도록 replace해줘` | `save_domain_metadata` 1회 |
| `eqp_uph 테이블의 query template과 컬럼을 등록해줘` | `save_table_catalog_metadata` 1회 |
| `OPER_NAME 필터 정의를 저장해줘` | `save_main_flow_filter_metadata` 1회 |
| `안녕, 어떤 일을 할 수 있어?` | Tool 없이 직접 안내 |
| `이거 처리해줘` | Tool 없이 구체적인 확인 질문 1회 |
