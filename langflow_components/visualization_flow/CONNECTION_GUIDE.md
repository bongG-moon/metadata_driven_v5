# 10. v5_html_visualization 연결 가이드

이 Flow는 Data Analysis가 MongoDB Result Store에 저장한 결과를 `result_ref`로 복원해 외부 CDN 없는 HTML/SVG 차트를 만듭니다. 차트 원본 행을 Workflow payload에 다시 복제하지 않습니다.

## 노드 구성

노드는 아래 4개만 사용합니다.

1. `Chat Input`
2. `00 HTML 시각화 생성기`
3. `Chat Output`
4. `01 HTML 시각화 API 종료 어댑터`

연결은 다음과 같습니다.

| 출발 출력 | 도착 입력 |
| --- | --- |
| `Chat Input.message` | `00 HTML 시각화 생성기.question` |
| `00 HTML 시각화 생성기.message` | `Chat Output.input_value` |
| `00 HTML 시각화 생성기.api_response` | `01 HTML 시각화 API 종료 어댑터.visualization_result` |

`01 HTML 시각화 API 종료 어댑터.api_response`가 다른 노드에 연결되지 않는 실제 terminal 출력입니다. Langflow는 component 단위로 terminal 여부를 판단하므로, 화면 Message가 연결된 생성기 노드의 API 포트를 임의로 terminal 취급하지 않습니다. Route V4의 이름 기반 Run Flow Tool은 이 별도 terminal `api_response`만 사용합니다.

## Standalone 입력값

| 입력 | 기본값 | 설명 |
| --- | --- | --- |
| `upstream_result_ref` | 빈 값 | 바로 앞 `run_data_analysis` 결과가 생성한 참조입니다. Route V4가 실행 시 전달합니다. |
| `mongo_uri` | Global Variable `MONGO_URL` | 화면에 보이는 standalone 입력입니다. 서버 환경변수에 암묵적으로 의존하지 않습니다. |
| `mongo_database` | `datagov` | Data Analysis Result Store와 같은 DB를 사용합니다. |
| `collection_name` | `agent_v4_result_store` | Data Analysis 결과 저장 컬렉션입니다. |
| `report_api_url` | `http://127.0.0.1:8010` | HTML을 게시하고 브라우저용 절대 URL을 반환하는 Report API 주소입니다. |
| `report_ttl_hours` | `24` | 보기·다운로드 링크 유효시간입니다. 1~168시간으로 제한합니다. |
| `max_chart_rows` | `500` | 초과 시 첫·끝을 포함한 균등 간격으로 표시 점을 줄입니다. |

`upstream_result_ref`를 가진 입력은 이 component 하나뿐이어야 합니다. Route V4가 import 후 실제 node ID를 찾아 이 입력에 참조를 전달합니다.

## 저장과 보기·다운로드

컴포넌트는 Langflow 1.8.2 저장 서비스에 아래 방식으로 HTML을 정확히 한 번 저장합니다.

```python
storage_service = get_storage_service()
await storage_service.save_file(
    flow_id=str(current_flow_id),
    file_name="html-chart-<uuid>.html",
    data=html_text.encode("utf-8"),
    append=False,
)
```

Message의 `files`와 API에는 `<flow_id>/<file_name>.html` 형식의 logical path를 호환용으로 유지합니다. 이 경로를 채팅 링크로 직접 사용하지는 않습니다. Langflow Desktop의 상대 주소는 `http://tauri.localhost/...`로 해석될 수 있기 때문입니다.

생성기는 같은 HTML을 `POST {report_api_url}/reports`로 한 번 게시하고 서버가 반환한 절대 `view_url`·`download_url`을 Message 본문에 Markdown 링크로 넣습니다.

```markdown
[HTML 차트 보기](http://127.0.0.1:8010/reports/view/<report-id>) · [HTML 다운로드](http://127.0.0.1:8010/reports/download/<report-id>)
```

Report API를 실행하지 않았거나 주소가 잘못되면 HTML의 Langflow 저장 자체는 유지하고 `status=partial`과 `report_api_publish_error` 경고를 반환합니다. 깨진 `tauri.localhost` 링크로 대체하지 않습니다.

Kubernetes에서는 `report_api_url`에 Langflow pod가 POST할 수 있는 Service 주소를 입력할 수 있습니다. 단, Report API의 `BASE_URL`은 사용자의 브라우저가 열 수 있는 사내 Ingress 주소여야 합니다. 내부 Service 이름을 `view_url`로 반환하면 사용자 PC에서는 열리지 않습니다.

## 출력 계약

`api_response`는 `visualization.result.v1`이며 `artifacts`에 다음 descriptor만 포함합니다.

```json
{
  "artifact_type": "html_chart",
  "path": "<flow_id>/html-chart-<uuid>.html",
  "report_id": "20260719010101_<uuid>",
  "view_url": "http://127.0.0.1:8010/reports/view/<report-id>",
  "download_url": "http://127.0.0.1:8010/reports/download/<report-id>",
  "expires_at": "2026-07-20T01:01:01+00:00",
  "ttl_hours": 24,
  "mime_type": "text/html",
  "title": "최근 3일 DIE ATTACH 공정 생산량을 그래프로 그려줘",
  "download_name": "html-chart-<uuid>.html",
  "chart_type": "line",
  "x_column": "WORK_DT",
  "y_columns": ["PRODUCTION"],
  "row_count": 3,
  "plotted_row_count": 3,
  "size_bytes": 8421
}
```

원본 `rows`, MongoDB 문서, HTML 본문은 Message/API/Workflow observation에 넣지 않습니다.

## Route V4 연결 규칙

`run_visualization`은 `accepts_upstream_result_ref=true`, `can_produce_result_ref=false`로 선언합니다. 시각화 단계는 정확히 하나의 Data Analysis 단계에 의존하고 `handoff=result_ref`를 사용해야 합니다.

```text
run_data_analysis
  -> result_ref
  -> run_visualization
  -> HTML artifact
```

시각화 Tool을 첫 단계로 실행하거나 요약 문자열을 데이터 대신 넘기는 fallback은 사용하지 않습니다.
