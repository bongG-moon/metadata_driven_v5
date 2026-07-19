# HTML 시각화 Flow 예시

## Route V4 권장 질문

```text
최근 3일 DIE ATTACH 공정 생산량을 조회하고 선 그래프로 그려줘.
```

Planner가 생성해야 하는 핵심 단계는 다음과 같습니다.

```json
{
  "contract_version": "workflow.plan.v1",
  "workflow_key": "inline",
  "title": "최근 3일 DIE ATTACH 생산량 시각화",
  "description": "생산량을 조회한 뒤 HTML 차트를 생성합니다.",
  "steps": [
    {
      "step_id": "production",
      "tool_name": "run_data_analysis",
      "question": "최근 3일 DIE ATTACH 공정 생산량을 일자별로 조회해.",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    },
    {
      "step_id": "visualization",
      "tool_name": "run_visualization",
      "question": "일자별 생산량을 선 그래프로 시각화해.",
      "depends_on": ["production"],
      "handoff": "result_ref",
      "on_error": "stop"
    }
  ]
}
```

등록된 Skill이 없어도 위 요청은 `workflow_key=inline`으로 실행할 수 있습니다.

## 차트 선택 예시

| 질문 | 기본 차트 | X축 선택 | Y축 선택 |
| --- | --- | --- | --- |
| `최근 3일 생산량을 선 그래프로 보여줘` | line | `WORK_DT`, `BASE_DT`, `DATE` 등 일자 컬럼 | `PRODUCTION` 또는 생산량 컬럼 |
| `제품별 재공 수량을 막대 그래프로 보여줘` | bar | 첫 제품/범주 컬럼 | `WIP`, `WIP_QTY`, 재공 수량 컬럼 |
| `일자별 INPUT 계획과 OUT 계획을 비교해줘` | line | 일자 컬럼 | 질문에 명시된 수치 컬럼 최대 3개 |

일자 값이 `20260718`처럼 숫자 모양이어도 컬럼명이 일자 컬럼이면 X축 문자열로 유지합니다. 정렬할 때만 datetime으로 해석하며 표시값을 숫자형으로 바꾸지 않습니다.

## 직접 Flow 테스트

1. `report_api` 폴더에서 `python server.py`를 실행하고 `http://127.0.0.1:8010/`에 `alive!`가 보이는지 확인합니다.
2. 같은 세션에서 Data Analysis Flow를 실행합니다.
3. Data Analysis의 terminal API 응답에서 `result_ref` 값을 확인합니다.
4. 10번 Flow의 `upstream_result_ref` 입력에 그 값을 넣습니다.
5. `HTML Report API 주소`가 `http://127.0.0.1:8010`인지 확인하고 같은 `session_id`로 실행합니다.
6. 답변의 `HTML 차트 보기`와 `HTML 다운로드`, terminal `api_response.artifacts[0]`을 확인합니다.

정상 응답의 최소 형태는 다음과 같습니다.

```json
{
  "contract_version": "visualization.result.v1",
  "response_type": "html_visualization",
  "status": "ok",
  "success": true,
  "message": "HTML 시각화를 생성했습니다.",
  "artifacts": [
    {
      "artifact_type": "html_chart",
      "path": "<flow-id>/html-chart-<uuid>.html",
      "report_id": "20260719010101_<uuid>",
      "view_url": "http://127.0.0.1:8010/reports/view/<report-id>",
      "download_url": "http://127.0.0.1:8010/reports/download/<report-id>",
      "expires_at": "2026-07-20T01:01:01+00:00",
      "ttl_hours": 24,
      "mime_type": "text/html",
      "title": "일자별 생산량을 선 그래프로 시각화해",
      "download_name": "html-chart-<uuid>.html",
      "chart_type": "line",
      "x_column": "WORK_DT",
      "y_columns": ["PRODUCTION"],
      "row_count": 3,
      "plotted_row_count": 3,
      "size_bytes": 8421
    }
  ],
  "warnings": [],
  "errors": []
}
```

## 차단되는 사례

- `upstream_result_ref`가 비어 있음
- 다른 session의 result_ref를 전달함
- Result Store에서 결과가 잘린 상태임
- 결과에 숫자형 지표 컬럼이 없음
- MongoDB 또는 Langflow 저장소를 사용할 수 없음

이 경우 정상 차트로 위장하지 않고 `status=error`, `artifacts=[]`와 구체적인 오류 유형을 반환합니다.

Report API만 연결되지 않은 경우는 차트 생성 자체가 성공했으므로 `status=partial`입니다. 이때 artifact의 Langflow `path`는 남지만 외부 `view_url`·`download_url`은 만들지 않습니다.
