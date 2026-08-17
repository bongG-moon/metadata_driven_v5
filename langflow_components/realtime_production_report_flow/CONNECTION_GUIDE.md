# 07-1. v5_realtime_production_report 연결 가이드

이 Flow는 질문에서 공정그룹을 먼저 확정한 뒤, 해당 그룹의 판정 데이터만 고정 Rule로 집계해 채팅 요약과 interactive HTML Report를 만듭니다. 또한 Report가 만든 결과 View를 Snapshot으로 저장하여 Flow 07-2의 안전한 후속 질문에 사용할 수 있습니다.

핵심 정책은 다음과 같습니다.

- `00C 결정론 Gate`가 Domain Metadata에 등록된 `process_groups`와 질문 원문만 사용해 단일 공정그룹을 선택합니다.
- 공정그룹 선택에는 LLM을 호출하지 않습니다.
- 세부 공정명은 최종 선택값이 아니지만, 어느 공정그룹인지 식별하는 근거로 사용할 수 있습니다.
- 질문에 공정그룹 근거가 없거나 두 그룹 이상이 함께 나타나면 Report를 만들지 않습니다.
- 미지정 시 전체 공정을 기본값으로 사용하지 않고, 사용자에게 분석할 공정그룹을 묻습니다.

## 노드 연결

```text
Chat Input
  -> 00C 공정그룹 선택 Gate.question
  -> 00D Realtime Report View Bundle 생성기.question
  -> 00E 공용 Report Context Publisher.question
  -> 01 Report 생성기.question

00A 공정그룹 카탈로그.process_group_catalog
  -> 00C 공정그룹 선택 Gate.process_group_catalog

00 실시간 생산 판정 더미 데이터.dataset
  -> 00C 공정그룹 선택 Gate.dataset

00C 공정그룹 선택 Gate.selected_dataset
  -> 00D Realtime Report View Bundle 생성기.dataset
  -> 01 실시간 생산 분석 Report 생성기.dataset

00D.report_bundle
  -> 00E 공용 Report Context Publisher.report_bundle

00E.context_payload
  -> 공용 MongoDB Result Store.payload

공용 MongoDB Result Store.payload_out
  -> 01 Report 생성기.context_payload

01 Report 생성기.api_response -> 공용 Session State Writer -> 02 API 종료 어댑터 -> Chat Output
```

`02 API 종료 어댑터.api_response`가 이름 기반 Run Flow Tool이 수집하는 terminal 출력입니다.

## 선택 동작

| 질문 | 결과 |
| --- | --- |
| `W/B 공정그룹 실시간 생산 분석 Report를 만들어줘` | `WB` 선택 후 W/B 세부 공정 행만 Report 생성 |
| `W/B2 실시간 생산 분석을 해줘` | `W/B2`가 속한 `WB` 그룹 선택 후 Report 생성 |
| `실시간 생산 분석 Report를 만들어줘` | `clarification_required`, HTML 미생성 |
| `W/B와 B/G를 분석해줘` | 단일 그룹 선택 요청, HTML 미생성 |
| `전체 공정을 분석해줘` | 단일 공정그룹을 다시 질문, HTML 미생성 |

`00C Gate`는 질문에 포함된 등록 alias·세부 공정명을 근거로 단일 그룹만 선택합니다. 질문 근거가 없거나 둘 이상의 그룹이 일치하면 clarification을 반환하며, 전체 공정을 기본값으로 추측하지 않습니다.

## 후속 질문용 View 발행

`00D`는 이 Report 전용 업무 계산을 수행해 `report.view.bundle.v1`을 만듭니다. 여기에는 `report_snapshot`과 `report_shortage_products`처럼 사람이 읽는 View 데이터·표시명·계산 결과만 담습니다.

`00E`는 그 Bundle을 읽어 실제 컬럼, 표시명, alias, 공개 범위, 단순 조회 허용 연산, 기본 View, lineage를 자동 계약으로 만들고 Result Store에 Snapshot을 저장합니다. 새로운 Report를 만들 때는 JSON 계약을 작성하는 대신 다음만 정하면 됩니다.

- 단순 Report: Report Data, 제목, 유형, 공개할 컬럼(선택)
- 여러 원천 Report: Recipe에서 조인·집계가 끝난 결과 View, View 표시명, 공개 여부와 lineage

원천 Evidence는 기본 View로 표시하지 않으면 자동으로 Flow 07-2에 공개되지 않습니다. 후속 질문에 추가로 열어 둘 완성된 View만 `followup_enabled=true`로 발행합니다.

## Domain Metadata 계약

`00A 공정그룹 카탈로그`는 별도 조회 방식 선택 없이 항상 `datagov.agent_v4_domain_items`의 MongoDB 문서를 조회합니다.

```json
{
  "_id": "domain:process_groups:WB",
  "section": "process_groups",
  "key": "WB",
  "status": "active",
  "payload": {
    "display_name": "W/B 공정 그룹",
    "aliases": ["WB", "W/B", "W/B 공정 그룹"],
    "field": "OPER_NAME",
    "processes": ["W/B1", "W/B2", "W/B3", "W/B4"]
  }
}
```

선택 결과는 `payload.field`와 `payload.processes`를 허용목록으로 사용해 판정 행을 필터링합니다. 모델이 필터 문자열을 직접 만들지 않습니다.

## 운영 전환

운영에서는 `00 실시간 생산 판정 더미 데이터`를 실제 Snapshot 로더로 교체하고, `00A 공정그룹 카탈로그`를 `mongodb` 모드로 전환합니다. 다음 `production.judgement.dataset.v1` 경계는 유지합니다.

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

Report Flow는 기존 판정식을 다시 계산하지 않습니다. 표시용 상위 분류만 생성합니다.

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

정상 선택 시 `realtime.production.report.v1`에는 선택 공정그룹, `report_scope`, `kpis`, `artifacts`, `warnings`, `errors`만 포함합니다. 원본 rows와 HTML 본문은 Workflow observation에 넣지 않습니다.

공정그룹 미지정 시에도 최상위 계약은 `realtime.production.report.v1`이며 아래 상태를 반환합니다.

```json
{
  "response_type": "realtime_production_process_group_clarification",
  "status": "clarification_required",
  "success": true,
  "artifacts": []
}
```

기본 `MongoDB Report API 주소`는 결과 다운로드와 HTML Report를 함께 제공하는 API_SERVER의 `http://127.0.0.1:5000`입니다. Flow가 실제 API_SERVER에 연결할 수 있는 주소로 이 값을 바꿉니다.

```powershell
cd API_SERVER
python app.py
```

HTML 본문과 제목·만료·권한 토큰 해시·보고서 계획 같은 metadata는 API_SERVER의 `report_save_db` 단일 MongoDB 컬렉션 문서에 함께 저장됩니다. Flow는 MongoDB URI를 직접 사용하지 않으며 Langflow 파일 저장소에 HTML을 중복 저장하지 않습니다.

`MongoDB Report API 주소`가 비어 있거나 API_SERVER 연결/저장에 실패하면 `status=error`와 빈 `artifacts`를 반환합니다. 공개 링크가 없는 로컬 HTML 파일은 남기지 않습니다. 단, 공정그룹 미지정 시에는 Report API를 호출하지 않습니다.
