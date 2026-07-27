# 11. v5_realtime_production_report 연결 가이드

이 Flow는 약 500행의 판정 더미 데이터를 생성하고, 생산실적·생산부족 원인·CAPA실적·장비Assign 조정을 고정 Rule로 집계해 채팅 요약과 interactive HTML Report를 만듭니다.

## 노드 연결

```text
Chat Input
  -> 01 실시간 생산 분석 Report 생성기.question

00 실시간 생산 판정 더미 데이터.dataset
  -> 01 실시간 생산 분석 Report 생성기.dataset

01 Report 생성기.message
  -> Chat Output

01 Report 생성기.api_response
  -> 02 API 종료 어댑터.report_result
```

`02 API 종료 어댑터.api_response`가 이름 기반 Run Flow Tool이 수집하는 terminal 출력입니다.

## 운영 전환

운영에서는 `00 실시간 생산 판정 더미 데이터`를 실제 Snapshot 로더로 교체합니다. 다음 `production.judgement.dataset.v1` 경계는 그대로 유지합니다.

```json
{
  "contract_version": "production.judgement.dataset.v1",
  "snapshot_id": "production:2026-07-27T14:30:00+09:00",
  "snapshot_at": "2026-07-27T14:30:00+09:00",
  "source_type": "live",
  "columns": [],
  "row_count": 500,
  "rows": []
}
```

Report Flow는 기존 판정식을 다시 계산하지 않습니다. 다만 표시용으로 다음 상위 분류를 생성합니다.

- `달성율*판정`: 정상·정상(초과생산) / Abnormal / 생산부족
- 생산부족 주원인: 적정재공부족 → CAPA부족 → 가동율저조 우선순위
- `CAPA이상판단`: 정상 / Abnormal / 생산부진1·생산부진2·CAPA부족
- 장비Assign 대상: CAPA실적이상 중 정상·교체불필요 / 장비필요 / 교체필요

## HTML 기능

- 네 개 Report 탭
- 영역별 Radio 필터
- 제품·공정 검색
- 핵심 컬럼/전체 컬럼 전환
- 현재 필터 CSV 다운로드
- 외부 CDN 없는 SVG Donut Chart

CSV에는 UTF-8 BOM을 넣어 Excel에서 한글이 깨지지 않게 합니다.

## 공개 API 계약

`realtime.production.report.v1`에는 `report_scope`, `kpis`, `artifacts`, `warnings`, `errors`만 포함합니다. 원본 rows와 HTML 본문은 Workflow observation에 넣지 않습니다.

기본 `HTML Report API 주소`는 `http://127.0.0.1:8765`입니다. 프로젝트 루트에서 아래 통합 서버를 실행하면 기존 data_ref CSV 다운로드와 Report 등록·보기·다운로드가 같은 주소에서 제공됩니다.

```powershell
python tools\data_ref_download_server.py --host 127.0.0.1 --port 8765
```

`HTML Report API 주소`가 비어 있거나 서버 연결에 실패하면 Langflow 저장소의 HTML은 남고 `status=partial`을 반환합니다.
