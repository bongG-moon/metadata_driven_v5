# Route Flow v2 Agent Tool 정의 (6종)

| Tool | 사용 범위 | 사용하지 않는 범위 |
| --- | --- | --- |
| `run_data_analysis` | 생산량, 재공, 투입/산출, HOLD, 장비 배정, UPH, 제품별 집계·비교 등 실제 값 | 메타데이터 정의 설명, 등록 |
| `run_metadata_qa` | 도메인, 데이터셋, 컬럼, SQL 템플릿, 필수 파라미터, 계산 규칙 확인 | 실제 제조 수치, 등록 |
| `save_domain_metadata` | 용어·별칭, 공정/제품 그룹, 분석 규칙의 등록·변경 | 테이블 스키마, 공통 필터 |
| `save_table_catalog_metadata` | source type, query template, 필수 파라미터, 컬럼 스키마의 등록·변경 | 도메인 용어, 공통 필터 |
| `save_main_flow_filter_metadata` | DATE, OPER_NAME, ORG 등 공통 필터 정의의 등록·변경 | 도메인, 테이블 카탈로그 |
| `run_realtime_production_report` | 질문에 `분석`이 있고 `실시간 생산 분석`·`실시간 분석`·`실시간 생산분석` 중 하나가 있는 고정 생산 Report 요청 | `분석`이 없는 실시간 현황 조회, 일반 생산 데이터 조회 |

모든 Tool은 사용자 원문을 node ID가 없는 필수 `question` 인자로 받고, 실행 직전에 현재 하위 Flow의 단일 Chat Input으로 내부 변환됩니다. 한 요청에서는 정확히 하나만 선택됩니다. 직전 문맥으로 해석되는 제조 데이터 후속 질문도 `run_data_analysis`를 사용하며, 실제 분석 상태 복원과 조건 상속은 해당 하위 Flow가 처리합니다.

`run_realtime_production_report` 노드는 Agent의 선택과 별개로 실행 직전에 같은 키워드 조건을 다시 검사합니다. 조건이 맞지 않으면 07-1번 Flow를 호출하지 않고 `키워드 차단 안내`를 직접 반환합니다. 공정그룹 선택과 누락 시 재질문은 07-1번 Flow가 담당합니다.
