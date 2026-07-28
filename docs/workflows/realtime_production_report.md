# 실시간 생산 분석 Report

## 목적

질문에 명시된 공정그룹을 Domain Metadata로 선택·검증한 뒤, 해당 그룹의 완료 생산 Snapshot을 한 번 읽고 다음 네 영역을 동일한 분모와 Rule 버전으로 분석한다.

1. 생산실적
2. 생산부족 원인
3. CAPA실적
4. 장비Assign 조정

## Workflow

```json
{
  "workflow_key": "realtime_production_report",
  "steps": [
    {
      "step_id": "realtime_production_report",
      "tool_name": "run_realtime_production_report",
      "question": "{{user_question}}",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    }
  ]
}
```

`{{user_question}}` 자리표시자는 Workflow 계획 파서가 사용자 원문으로 치환합니다. 따라서 W/B, B/G 같은 공정그룹 표현이 하위 Flow까지 보존됩니다. 원문에 공정그룹이 없으면 하위 Flow가 `clarification_required`를 반환하고 HTML을 만들지 않습니다.

07 Agent Tool Router를 통한 단일 호출에서는 질문에 `분석`이 포함되고 `실시간 생산 분석`·`실시간 분석`·`실시간 생산분석` 중 하나가 있을 때만 이 Tool을 선택합니다. 같은 조건은 Tool 실행 직전에도 다시 검증하므로 `실시간 생산 현황을 보여줘`만으로는 11번 Flow가 실행되지 않습니다.

예시 Flow는 W/B, B/G, D/A 공정그룹에 걸친 결정론적 더미 데이터 약 500행을 내부에서 생성한다. 운영 전환 시 공정그룹 카탈로그를 MongoDB 모드로 바꾸고 더미 생성 노드를 실제 판정 Snapshot 로더로 교체하며 Report 생성기의 `production.judgement.dataset.v1` 입력 계약은 유지한다.

HTML 등록·보기·다운로드는 기본 `http://127.0.0.1:8765`의 `tools/data_ref_download_server.py`가 담당한다. 이 서버는 Data Analysis의 data_ref CSV 다운로드도 함께 제공한다.

## 검증 질문

- 오늘 W/B 공정 실시간 생산 분석 Report를 만들어줘.
- B/G 실시간 분석을 해줘.
- D/A 실시간 생산분석 해줘.
- 생산부족 원인과 CAPA실적을 분석하고 장비Assign 조정 대상도 보여줘.
- W/B1~W/B4 판정 데이터를 Report로 만들어줘.

## 오류 정책

- 필수 판정 컬럼 누락: 중단
- 공정그룹 미지정 또는 다중 지정: Report 미생성, 단일 공정그룹 재질문
- LLM 선택과 질문 원문 근거 불일치: 중단
- 미등록 판정값: 미분류 및 경고
- Report API 게시 실패: Langflow HTML 파일을 보존하고 `partial` 반환
- HTML 표시 행 상한 초과: 전체 집계는 유지하고 표 행만 제한
