# 실시간 생산 분석 Report Workflow

## 목적

완료된 생산 판정 Snapshot을 고정 Rule로 집계하고 다음 네 영역을 하나의 채팅 요약과 interactive HTML Report로 제공한다.

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
      "question": "현재 판정 Snapshot으로 생산실적, 생산부족 원인, CAPA실적, 장비Assign 조정을 분석하고 상세 HTML Report를 생성해.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    }
  ]
}
```

이 Workflow는 전용 `run_realtime_production_report` 한 단계만 실행한다. 생산 판정 데이터, 고정 집계 Rule, 채팅 응답, HTML Report 게시를 전용 Flow가 소유하므로 선행 `run_data_analysis`나 후행 `run_visualization`을 연결하지 않는다.

예시 Flow는 결정론적 더미 판정 데이터 약 500행을 내부에서 생성한다. 운영 전환 시 더미 생성 노드를 실제 판정 Snapshot Loader로 교체하며 Report 생성기의 `production.judgement.dataset.v1` 입력 계약은 유지한다.

HTML 등록·보기·다운로드는 기본 `http://127.0.0.1:8765`의 `tools/data_ref_download_server.py`가 담당한다. 이 서버는 Data Analysis의 data_ref CSV 다운로드도 함께 제공한다.

## 검증 질문

- 오늘 W/B 공정 실시간 생산 분석 Report를 만들어줘.
- 생산부족 원인과 CAPA실적을 분석하고 장비Assign 조정 대상도 보여줘.
- W/B1~W/B4 판정 데이터를 Report로 만들어줘.

## 오류 정책

- 필수 판정 컬럼 누락: 중단
- 미등록 판정값: 경고
- Report API 게시 실패: Langflow 저장소의 HTML은 보존하고 `partial` 반환
- HTML 표시 행 상한 초과: 전체 집계는 유지하고 상세 표만 제한

실제 운영 전환 절차는 `docs/REALTIME_PRODUCTION_REPORT_PRODUCTION_IMPLEMENTATION_GUIDE.md`를 따른다.
