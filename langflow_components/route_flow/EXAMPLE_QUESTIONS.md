# Route Flow 예시 질문 (06 API Router)

아래 질문으로 Smart Router route와 API 메시지 호출 계약을 확인합니다.

| 질문 | 기대 route | 확인할 점 |
| --- | --- | --- |
| 오늘 DA공정 생산량 알려줘 | `data_analysis` | `01` API payload의 `input_value`가 질문 원문과 동일하다. |
| L-114제품 생산량 알려줘 | `data_analysis` | router는 제품 token을 해석하지 않고 data analysis flow로 원문을 보낸다. |
| 현재 조회 가능한 dataset list와 필수 para정보를 알려줘 | `metadata_qa` | metadata QA flow API가 호출되고 실제 답변이 `01.message`로 나온다. |
| production_today 필수 조건 보여줘 | `metadata_qa` | dataset 필수 파라미터 질문이 metadata QA로 분기된다. |
| metadata 종류를 알려줘 | `metadata_qa` | 메타데이터 안내 질문이 metadata QA로 분기된다. |
| 생산량 데이터 관련 쿼리문은 어떤 건지 알려줘 | `metadata_qa` | SQL/metadata 질문이 metadata QA로 분기된다. |
| DA 공정 그룹을 D/A1~D/A6로 등록해줘 | `domain_saving` | 저장 원문이 하위 saving flow에 그대로 전달된다. |
| production_today 데이터셋을 등록해줘 | `table_catalog_saving` | query template, `WITH`, `--` 주석이 손상되지 않는다. |
| DATE 필터를 YYYYMMDD 형식 필수 파라미터로 등록해줘 | `main_flow_filter_saving` | main flow filter saving flow가 선택된다. |
| 안녕 | `direct_answer` | 하위 flow API를 호출하지 않고 안내 메시지를 반환한다. |
| 이거 확인해줘 | `clarification` | 하위 flow API를 호출하지 않고 추가 정보를 요청한다. |

## 잘못된 설정 확인 예시

API 호출 route의 Route Message에 아래처럼 JSON을 넣으면 안 됩니다.

```json
{"route":"data_analysis"}
```

이 경우 하위 flow에는 사용자 질문이 아니라 위 JSON이 들어갈 수 있습니다.
API 호출 route의 Route Message는 비워두고, route 분류는 Route Name/Description만으로 처리합니다.
