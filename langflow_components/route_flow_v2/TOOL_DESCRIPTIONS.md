# Route Flow v2 Agent Tool 정의 (07)

| Tool | 사용 범위 | 사용하지 않는 범위 |
| --- | --- | --- |
| `run_data_analysis` | 생산량, 재공, 투입/산출, HOLD, 장비 배정, UPH, 제품별 집계·비교 등 실제 값 | 메타데이터 정의 설명, 등록 |
| `run_metadata_qa` | 도메인, 데이터셋, 컬럼, SQL 템플릿, 필수 파라미터, 계산 규칙 확인 | 실제 제조 수치, 등록 |
| `save_domain_metadata` | 용어·별칭, 공정/제품 그룹, 분석 규칙의 등록·변경 | 테이블 스키마, 공통 필터 |
| `save_table_catalog_metadata` | source type, query template, 필수 파라미터, 컬럼 스키마의 등록·변경 | 도메인 용어, 공통 필터 |
| `save_main_flow_filter_metadata` | DATE, OPER_NAME, ORG 등 공통 필터 정의의 등록·변경 | 도메인, 테이블 카탈로그 |

모든 Tool은 사용자 원문을 한 개의 Chat Input 인자로 받고, 한 요청에서 정확히 하나만 선택됩니다.
