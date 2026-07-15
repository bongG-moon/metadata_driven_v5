# Workflow Skill 저장 Flow

Route V4에서 반복 실행할 업무 순서를 자연어로 등록하고 MongoDB `datagov.agent_v4_workflow_skills`에 저장하는 standalone Flow 소스입니다.

## 저장 문서 계약

```json
{
  "_id": "workflow:wb_daily_production_metadata",
  "section": "workflow_skills",
  "key": "wb_daily_production_metadata",
  "status": "active",
  "payload": {
    "display_name": "WB 당일 생산량과 공정 그룹 정의 조회",
    "description": "생산량을 조회한 뒤 등록된 공정 그룹 정의를 조회합니다.",
    "aliases": ["WB 생산량과 메타데이터"],
    "intent_examples": ["오늘 WB 생산량과 등록된 WB 공정 정의를 함께 알려줘"],
    "keywords": ["WB", "생산량", "공정 그룹"],
    "excluded_keywords": [],
    "priority": 100,
    "steps": [
      {
        "step_id": "production",
        "tool_name": "run_data_analysis",
        "question": "오늘 WB 공정 생산량을 조회해.",
        "depends_on": [],
        "handoff": "none",
        "on_error": "stop"
      }
    ]
  },
  "updated_at": "ISO-8601 UTC",
  "registration_trace": {
    "raw_text": "마스킹·길이 제한이 적용된 등록 원문"
  }
}
```

`revision`, `request_id`, `idempotency` 필드는 사용하지 않습니다.

## 핵심 정책

- `dry_run=true`가 기본값입니다. 검수 결과를 확인한 뒤에만 `false`로 전환합니다.
- Workflow는 요청당 1건, 단계는 1~4개입니다.
- 각 단계는 하나의 Tool만 호출하고 `depends_on`은 앞 단계만 참조합니다.
- `handoff=result_ref`는 한 개의 `run_data_analysis` 결과를 다음 `run_data_analysis`에 전달할 때만 허용합니다.
- LLM은 후보 초안만 만들며 Tool·dependency·handoff·중복 처리·실제 저장은 Python에서 결정합니다.
- 모호한 유사 항목을 첫 번째 문서로 임의 선택하지 않습니다.

## 중복 처리

| 옵션 | 유사 항목 1건 | 유사 항목 없음 | 유사 항목 여러 건 |
| --- | --- | --- | --- |
| `skip` | 기존 유지 | 신규 저장 | 차단 |
| `merge` | 기존 canonical 문서에 병합 | 신규 저장 | 차단 |
| `replace` | 기존 canonical 문서를 교체 | 신규 저장 | 차단 |
| `create_new` | 충돌 없는 새 key로 저장 | 신규 저장 | 새 key로 저장 |

`replace`는 “기존 문서가 없으면 실패”가 아닙니다. 유사 항목이 하나면 그 문서를 교체하고, 없으면 새 Workflow Skill로 저장합니다.

## 파일 구성

- `00_workflow_skill_saving_request_loader.py`: 입력·dry-run·중복 정책 초기화
- `00_workflow_skill_existing_items_loader.py`: 기존 MongoDB 문서 조회
- `03_workflow_skill_saving_variables_builder.py`: LLM 입력 최소화
- `03_saving_prompt_template_ko.md`: 후보 생성 Prompt
- `04_workflow_skill_saving_result_normalizer.py`: 실행 계약 정규화·검증
- `05_workflow_skill_similarity_checker.py`: 유사 canonical 대상 조회
- `07_workflow_skill_review_writer.py`: 결정론적 검수와 MongoDB 저장
- `08_workflow_skill_saving_response_builder.py`: compact 응답 생성
- `09_workflow_skill_saving_message_adapter.py`: 단일 Markdown 메시지
- `10_workflow_skill_saving_api_response_builder.py`: Web·Run API 응답
- `CONNECTION_GUIDE.md`: Langflow 연결과 설정
- `INPUT_EXAMPLES.md`: 바로 실행할 입력 예시
