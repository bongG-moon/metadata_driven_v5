# metadata saving rev_2 import bundle

이 디렉터리는 기존 02/03/04 저장 Flow를 교체하지 않는 독립 검증용 rev_2 Flow 3개를 포함합니다. 기존 9개 complete bundle과 Flow 06 Router는 변경되지 않습니다.

## Import

Langflow Desktop 1.11.0에서 `00_metadata_saving_rev_2_ALL_FLOWS.json` 하나를 import하거나 아래 파일을 개별 import합니다.

| 번호 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
| 02 | `02_domain_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-domain-saving-rev-2` | 20 | 28 |
| 03 | `03_table_catalog_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-table-catalog-saving-rev-2` | 20 | 28 |
| 04 | `04_main_flow_filter_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-main-flow-filter-saving-rev-2` | 20 | 28 |

## 동작 계약

- 기본값은 기존과 동일하게 Dry Run입니다.
- `MONGO_URL` Credential Global Variable과 기존 `datagov` 컬렉션 3종을 읽어 활성 계약을 확인합니다.
- 사용자 원문, Flow 정제안, 확정된 dataset/표준 컬럼 변환은 응답에 분리되어 표시됩니다.
- 모호하거나 등록되지 않은 참조는 `needs_input`으로 저장 0건 처리하고 복사 가능한 재입력 예시를 반환합니다.
- 실제 저장은 기존 writer를 그대로 사용하므로 collection, `_id`, item payload, `registration_trace.raw_text` 형태는 기존과 같습니다.
- Sub Agent 내부에서 HITL resume을 사용하지 않습니다. 보완 응답을 받은 사용자가 입력을 수정해 새 요청으로 다시 실행합니다.
- Router Tool은 계속 기존 02/03/04 Flow를 가리킵니다. rev_2 운영 전환은 별도 검증 후 명시적으로 수행해야 합니다.
