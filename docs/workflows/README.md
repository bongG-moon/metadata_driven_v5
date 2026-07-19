# 08 Workflow Orchestrator 업무 문서 작성 가이드

이 폴더는 08 Workflow Orchestrator가 실행할 반복 업무를 사람이 검토할 수 있는 문서와 seed registry JSON으로 함께 관리하는 예시다. 문서 파일을 런타임에서 직접 읽지는 않는다. 운영 Workflow는 `09 Workflow Skill 저장 Flow`로 `datagov.agent_v4_workflow_skills`에 저장하고, 08의 `00A Workflow Registry 로더`가 다음 실행부터 질문 관련 후보를 조회한다.

## 권장 폴더 구성

```text
docs/workflows/
├─ README.md
├─ workflow_registry.example.json
├─ daily_manufacturing_briefing.md
├─ hold_lot_history_metadata_audit.md
└─ equipment_uph_source_audit.md
```

- `README.md`: 공통 작성 규칙과 등록 절차
- `workflow_registry.example.json`: `inline_seed` 로컬 검증과 저장 입력 작성의 기준본
- `<workflow_key>.md`: 업무 목적, 단계별 질문, 의존성, 오류 정책과 검증 질문을 설명하는 검토 문서

문서 이름과 `workflow_key`는 같게 유지한다. 운영 registry에서 key를 바꾸면 Markdown 파일명과 테스트 질문도 함께 바꾼다.

## 단계 작성 원칙

각 Workflow는 1~4단계만 허용하며, 단계는 문서에 적힌 순서대로 한 번에 하나씩 실행된다.

| 필드 | 필수 | 의미 |
| --- | --- | --- |
| `step_id` | 예 | Workflow 안에서 중복되지 않는 영문 단계 ID |
| `tool_name` | 예 | 08 Workflow Orchestrator에 연결된 정확한 Tool 이름 |
| `question` | 예 | 해당 하위 Flow가 독립적으로 이해할 수 있는 완전한 질문 |
| `depends_on` | 예 | 먼저 성공해야 하는 앞 단계 ID 목록 |
| `handoff` | 예 | 순서만 보장하면 `none`, 실제 앞 단계 데이터가 필요하면 `result_ref` |
| `on_error` | 예 | 실패 후 전체를 중단하면 `stop`, 독립 단계만 계속하려면 `continue` |

08 자체에는 다음 여섯 Tool이 연결되어 있다.

- `run_data_analysis`
- `run_metadata_qa`
- `save_domain_metadata`
- `save_table_catalog_metadata`
- `save_main_flow_filter_metadata`
- `run_visualization`

현재 권장 Registry 3종은 실제 조회 조합을 우선 검증하기 위해 `run_data_analysis`와 `run_metadata_qa`만 사용한다. 기본 Language Model은 Skill Tool이 아니라 모든 단계가 끝난 뒤 자동으로 실행되는 최종 합성 노드이므로 `steps`에 넣지 않는다. 저장 Tool을 사용하는 별도 Workflow를 만들 수는 있지만, 이번 seed와 사용자용 등록 예시에는 포함하지 않는다.

`depends_on`과 `handoff`는 서로 다른 개념이다. 예를 들어 “생산량을 조회한 뒤 데이터셋 정의를 조회”는 실행 순서만 필요하므로 `depends_on=["production"]`, `handoff="none"`이다. 반대로 “현재 HOLD LOT을 조회한 뒤 그 LOT의 HOLD 이력 조회”는 앞 단계의 실제 LOT 목록이 필요하므로 `handoff="result_ref"`를 사용한다.

“최근 3일 생산량을 조회한 뒤 그래프로 표시”는 `run_data_analysis`가 만든 실제 결과가 필요하므로 `run_visualization` 단계가 분석 단계 하나를 `depends_on`으로 지정하고 `handoff="result_ref"`를 사용한다. 자주 반복하지 않는 작은 조합은 Registry에 미리 저장하지 않아도 08의 inline 계획으로 실행할 수 있다.

## 자연어 업무 입력 예시

08 Playground에는 다음처럼 사람이 작성한 업무 절차를 그대로 입력할 수 있다. 첫 Language Model이 이 내용을 `workflow.plan.v1` JSON으로 변환하고, 검증 파서가 Tool 이름·의존 순서·최대 4단계를 확인한 뒤에만 Loop를 시작한다.

```text
아래 업무를 순서대로 진행해.

1. 오늘 DA 공정 생산량을 조회해.
2. 1번 실행이 완료된 뒤 현재 DA 공정 재공을 조회해.
3. 2번 실행이 완료된 뒤 두 조회에 사용된 데이터셋 정의를 확인해.
4. 각 단계에서 한 번에 하나의 Tool만 호출하고 병렬로 호출하지 마.
5. 모든 실행이 끝난 뒤 결과를 하나의 답변으로 정리해줘.
```

자주 쓰는 업무는 자연어를 매번 다시 작성하는 대신 registry의 정확한 key를 입력할 수 있다.

```text
daily_manufacturing_briefing
```

## 신규 Workflow 등록 절차

1. 기존 예시를 복사해 `<workflow_key>.md`를 작성한다.
2. 각 단계 질문이 하위 Flow 단독 실행에서도 의미가 있는지 확인한다.
3. 실제 데이터 전달이 필요한 단계만 `handoff=result_ref`로 지정한다.
4. `09 Workflow Skill 저장 Flow`에서 `dry_run=true`로 자연어 등록 입력을 실행한다.
5. 생성된 Tool·단계·dependency·handoff를 확인한 뒤 `dry_run=false`로 다시 실행한다.
6. 08의 `registry_source=mongodb`, `MONGO_URL`, database/collection 입력이 저장 Flow와 같은지 확인한다.
7. 등록 key 실행, 자연어 intent example 실행, 실패 정책을 각각 테스트한다.

바로 사용할 자연어 입력은 `langflow_components/workflow_skill_saving_flow/INPUT_EXAMPLES.md`에 있다. `workflow_registry.example.json`을 수정해 빌드하는 방식은 `inline_seed` 회귀 테스트가 필요할 때만 사용한다.

```powershell
python tools\build_v5_auxiliary_flows.py
python tools\build_import_ready_bundle.py
python -m pytest -q --basetemp=.pytest_tmp
python tools\validate_flow_component_sources.py
```

## 변경 검토 체크리스트

- 4단계를 초과하지 않았는가
- 모든 `tool_name`이 08 Workflow Orchestrator에 실제 연결되어 있는가
- `depends_on`은 항상 앞 단계만 가리키는가
- `result_ref`가 필요한 Tool이 해당 입력을 지원하는가
- 현재 권장 3종이 `run_data_analysis`, `run_metadata_qa`만 사용하는가
- 단계 질문에 날짜·공정·대상 등 필수 조건이 빠지지 않았는가
- 실패 시 중단할지 독립 단계만 계속할지가 문서와 JSON에서 일치하는가
- 최종 답변은 마지막 Language Model에서 한 번만 생성되는가
