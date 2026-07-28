# 11. v5_realtime_production_report 연결 가이드

이 Flow는 질문에서 공정그룹을 먼저 확정한 뒤, 해당 그룹의 판정 데이터만 고정 Rule로 집계해 채팅 요약과 interactive HTML Report를 만듭니다.

핵심 정책은 다음과 같습니다.

- LLM은 Domain Metadata에 등록된 `process_groups` 중 하나만 선택합니다.
- LLM의 선택은 질문 원문과 허용목록을 사용해 `00C Gate`가 다시 검증합니다.
- 세부 공정명은 최종 선택값이 아니지만, 어느 공정그룹인지 식별하는 근거로 사용할 수 있습니다.
- 질문에 공정그룹 근거가 없거나 두 그룹 이상이 함께 나타나면 Report를 만들지 않습니다.
- 미지정 시 전체 공정을 기본값으로 사용하지 않고, 사용자에게 분석할 공정그룹을 묻습니다.

## 노드 연결

```text
Chat Input
  -> GaiA Input Adapter
     -> 00B 공정그룹 선택 Prompt.question
     -> 00C 공정그룹 선택 Gate.question
     -> 01 Report 생성기.question

00A 공정그룹 카탈로그.process_group_catalog
  -> 00B 공정그룹 선택 Prompt.process_group_catalog
  -> 00C 공정그룹 선택 Gate.process_group_catalog

00B 공정그룹 선택 Prompt.prompt
  -> Language Model.input_value

Language Model.text_output
  -> 00C 공정그룹 선택 Gate.llm_response

00 실시간 생산 판정 더미 데이터.dataset
  -> 00C 공정그룹 선택 Gate.dataset

00C 공정그룹 선택 Gate.selected_dataset
  -> 01 실시간 생산 분석 Report 생성기.dataset

01 Report 생성기.message
  -> GaiA Output Adapter
  -> Chat Output

01 Report 생성기.api_response
  -> 02 API 종료 어댑터.report_result
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

LLM 출력은 아래 JSON object 하나로 제한됩니다.

```json
{
  "status": "selected",
  "process_group_key": "WB",
  "reason": "질문에 W/B 공정그룹이 명시되었습니다.",
  "evidence": ["W/B"]
}
```

`00C Gate`는 LLM이 `WB`를 선택해도 질문에 W/B 관련 표현이 실제로 없으면 통과시키지 않습니다.

## Domain Metadata 계약

예시 Flow의 `00A 공정그룹 카탈로그`는 `inline_json` 모드로 W/B, B/G, D/A 예시를 제공합니다. 운영에서는 `source_mode=mongodb`로 바꾸고 아래 문서를 `datagov.agent_v4_domain_items`에서 조회합니다.

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

선택 결과는 `payload.field`와 `payload.processes`를 허용목록으로 사용해 판정 행을 필터링합니다. LLM이 필터 문자열을 직접 만들지 않습니다.

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

기본 `HTML Report API 주소`는 `http://127.0.0.1:8765`입니다.

```powershell
python tools\data_ref_download_server.py --host 127.0.0.1 --port 8765
```

`HTML Report API 주소`가 비어 있거나 서버 연결에 실패하면 Langflow 저장소의 HTML은 남고 `status=partial`을 반환합니다. 단, 공정그룹 미지정 시에는 저장소와 Report API를 호출하지 않습니다.
