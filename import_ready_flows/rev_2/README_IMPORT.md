# metadata saving rev_2 import bundle

이 디렉터리는 기존 02/03/04 저장 Flow를 교체하지 않는 독립 검증용 rev_2 Flow 3개를 포함합니다. 기존 9개 complete bundle과 Flow 06 Router는 변경되지 않습니다.

## Import

Langflow Desktop 1.11.0에서 `00_metadata_saving_rev_2_ALL_FLOWS.json` 하나를 import하거나 아래 파일을 개별 import합니다.

| 번호 | 파일 | endpoint_name | 실행 노드 | 설명 Note | 엣지 |
| ---: | --- | --- | ---: | ---: | ---: |
| 02 | `02_domain_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-domain-saving-rev-2` | 20 | 5 | 28 |
| 03 | `03_table_catalog_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-table-catalog-saving-rev-2` | 17 | 5 | 20 |
| 04 | `04_main_flow_filter_saving_flow_v5_rev_2_standalone.json` | `metadata-driven-v5-main-flow-filter-saving-rev-2` | 17 | 5 | 20 |

## 동작 계약

- 기본값은 기존과 동일하게 테스트 실행입니다. 테스트 실행 중에는 MongoDB에 저장하지 않습니다.
- `02` Domain rev_2만 `MONGO_URL` Credential Global Variable과 기존 `datagov` 컬렉션을 읽어 활성 계약을 확인합니다.
- `03` Table Catalog와 `04` Main Flow Filter rev_2는 각 기존 저장 경로에 비차단 초기 문장 변환과 출력 전용 Portal 계약 보강만 추가한 경로입니다. 초기 변환은 사용자가 직접 쓴 실행/필터 계약을 보존하며, snapshot·후보 복구·공통 Contract Guard로 저장을 선차단하지 않습니다.
- 실제 저장 가능 여부는 각 Flow의 기존 normalizer와 writer가 판단합니다. `03`과 `04`는 기존 필수 검증과 중복 처리 정책만 적용합니다.
- 실제 저장은 기존 writer를 그대로 사용하므로 collection, `_id`, item payload, `registration_trace.raw_text` 형태는 기존과 같습니다.
- `03`과 `04`의 API terminal은 Portal 계약인 `status`, `data`, `metadata_authoring`, `write_result`, `trace`를 유지합니다. Portal 보강기는 Writer의 status·message·저장 결과를 변경하지 않습니다.
- 각 Flow에는 실행과 연결되지 않은 5개 설명 Note가 있습니다. Note는 캔버스 안내용이며 실행 노드·엣지·저장 동작에는 영향을 주지 않습니다.
- Sub Agent 내부에서 HITL resume을 사용하지 않습니다. 보완 응답을 받은 사용자가 입력을 수정해 새 요청으로 다시 실행합니다.
- Router Tool은 계속 기존 02/03/04 Flow를 가리킵니다. rev_2 운영 전환은 별도 검증 후 명시적으로 수행해야 합니다.
