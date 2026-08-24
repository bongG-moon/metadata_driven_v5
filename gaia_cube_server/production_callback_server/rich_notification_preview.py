"""Create a local, no-network preview of GAIA -> CUBE Rich Notification rendering.

Run this file from this folder:

    python rich_notification_preview.py

It never loads credentials or calls GAIA/CUBE.  It uses representative GAIA
response envelopes, extracts the same final answer used by the callback
server, then writes JSON and a browser-readable HTML preview.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from app import extract_final_answer, render_gaia_answer_to_cube_body


OUTPUT_DIRECTORY = Path(__file__).with_name("preview_output")


def _gaia_response(answer: str) -> dict[str, Any]:
    """Make the relevant part of a real GAIA/Langflow response envelope."""

    return {
        "session_id": "preview-session",
        "outputs": [
            {
                "outputs": [
                    {
                        "component_display_name": "Chat Output",
                        "component_id": "ChatOutput-preview",
                        "results": {
                            "gaia_response": {"data": {"answer": answer}},
                            "message": {"data": {"error": False, "text": answer}},
                        },
                    }
                ]
            }
        ],
    }


def preview_examples() -> list[dict[str, Any]]:
    """Return examples that match common Langflow answer forms."""

    raw_examples = [
        {
            "id": "dataset_list",
            "title": "데이터셋 목록 답변",
            "source": "GAIA answer에 제목, 목록, Markdown 표가 함께 있는 경우",
            "answer": """### 답변
현재 등록된 조회 데이터셋은 총 3개입니다.

### 한눈에 보기
- 연결 방식: Oracle 2개, Goodocs 1개
- 필수 조건이 있는 데이터셋은 1개입니다.

### 조회 가능한 데이터
| 데이터셋 | 데이터셋 키 | 연결 방식 | 필수 조건 |
| --- | --- | --- | --- |
| Equipment UPH | eqp_uph | Oracle | 없음 |
| HOLD History | hold_history | Oracle | LOT_ID |
| PKG Target Plan | target | Goodocs | 없음 |

총 3건입니다.""",
        },
        {
            "id": "download_link",
            "title": "보고서/다운로드 링크 답변",
            "source": "GAIA answer에 Markdown 또는 HTML 링크가 있는 경우",
            "answer": """### 분석 결과
요청하신 생산 현황을 준비했습니다.

📥 [CSV 다운로드](https://example.test/reports/production.csv)
🔗 <a href="https://example.test/reports/detail">상세 분석 화면 열기</a>

위 링크는 CUBE에서 클릭 가능한 hypertext로 변환됩니다.""",
        },
        {
            "id": "html_guidance",
            "title": "HTML 줄바꿈과 안내 메시지",
            "source": "HTML 문단/줄바꿈과 주의·오류·추가 조건 안내가 함께 있는 경우",
            "answer": """<p>조회 결과를 안내드립니다.</p>
<p>주의: 수치는 잠정 집계값입니다.<br>확정 수치는 마감 후 다시 확인해 주세요.</p>
<p>추가 조건 필요: 조회 날짜를 입력해 주세요.</p>
<p>오류: 조회 서버에 연결할 수 없습니다.</p>""",
        },
    ]

    examples: list[dict[str, Any]] = []
    for example in raw_examples:
        answer = extract_final_answer(_gaia_response(example["answer"]))
        examples.append(
            {
                "id": example["id"],
                "title": example["title"],
                "source": example["source"],
                "gaia_answer": answer,
                "cube_body": render_gaia_answer_to_cube_body(answer),
            }
        )
    return examples


def _control_text(column: dict[str, Any]) -> str:
    text = column.get("control", {}).get("text", [""])
    return str(text[0]) if isinstance(text, list) and text else ""


def _render_row(row: dict[str, Any]) -> str:
    """Render a non-table CUBE row in the local preview only."""

    columns = row.get("column", [])
    if len(columns) != 1:
        return ""
    column = columns[0]
    text = html.escape(_control_text(column))
    if column.get("type") == "hypertext":
        url = html.escape(str(column.get("control", {}).get("linkurl", "")), quote=True)
        return f'<p class="link-row"><a href="{url}" target="_blank" rel="noreferrer">{text}</a></p>'
    color = html.escape(str(column.get("control", {}).get("color", "#000000")))
    bgcolor = html.escape(str(column.get("bgcolor", "#ffffff")))
    border = str(column.get("border", "false")).lower() == "true"
    border_css = "1px solid #d0d7de" if border else "0"
    return (
        '<p class="label-row" '
        f'style="color:{color};background:{bgcolor};border:{border_css}">{text}</p>'
    )


def _render_cube_body(body: dict[str, Any]) -> str:
    """Turn generated CUBE rows into a readable approximation for a browser."""

    parts: list[str] = []
    table_rows: list[dict[str, Any]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        headers = table_rows[0].get("column", [])
        head = "".join(f"<th>{html.escape(_control_text(column))}</th>" for column in headers)
        body_rows = []
        for row in table_rows[1:]:
            cells = "".join(
                f"<td>{html.escape(_control_text(column))}</td>"
                for column in row.get("column", [])
            )
            body_rows.append(f"<tr>{cells}</tr>")
        parts.append(
            '<table><thead><tr>'
            + head
            + "</tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table>"
        )
        table_rows = []

    for row in body.get("row", []):
        if len(row.get("column", [])) > 1:
            table_rows.append(row)
            continue
        flush_table()
        parts.append(_render_row(row))
    flush_table()
    return "".join(parts) or "<p>표시할 CUBE row가 없습니다.</p>"


def build_preview_html(examples: list[dict[str, Any]]) -> str:
    """Build a self-contained HTML page; all user-visible strings are escaped."""

    cards: list[str] = []
    for example in examples:
        cards.append(
            "<section class=\"card\">"
            f"<h2>{html.escape(example['title'])}</h2>"
            f"<p class=\"hint\">{html.escape(example['source'])}</p>"
            "<h3>1. GAIA가 추출한 최종 answer</h3>"
            f"<pre>{html.escape(example['gaia_answer'])}</pre>"
            "<h3>2. CUBE에서 보일 형태 (로컬 미리보기)</h3>"
            f"<div class=\"cube\">{_render_cube_body(example['cube_body'])}</div>"
            "<h3>3. CUBE로 보낼 body JSON</h3>"
            "<pre>"
            + html.escape(
                json.dumps(example["cube_body"], ensure_ascii=False, indent=2)
            )
            + "</pre>"
            "</section>"
        )

    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GAIA → CUBE Rich Notification 변환 미리보기</title>
  <style>
    body { background:#f5f7fa; color:#17202a; font-family:Segoe UI, Malgun Gothic, sans-serif; margin:0; }
    main { max-width:1100px; margin:0 auto; padding:36px 20px 64px; }
    h1 { margin-bottom:8px; } .lead, .hint { color:#52616b; }
    .card { background:#fff; border:1px solid #dbe3ea; border-radius:12px; margin-top:22px; padding:24px; box-shadow:0 2px 8px #17202a0c; }
    h2 { color:#1f4e79; margin-top:0; } h3 { font-size:15px; margin:25px 0 8px; }
    pre { background:#17202a; color:#eaf2f8; border-radius:8px; padding:14px; overflow:auto; white-space:pre-wrap; line-height:1.55; }
    .cube { border:1px solid #ccd6df; border-radius:8px; padding:10px 16px; background:#fff; }
    .label-row, .link-row { border-bottom:1px solid #edf1f4; margin:0; padding:10px 2px; line-height:1.45; }
    .link-row a { color:#1264a3; font-weight:600; } table { border-collapse:collapse; width:100%; margin:10px 0; } th, td { border:1px solid #bac6d1; text-align:left; padding:8px; } th { background:#f2f2f2; color:#1f4e79; }
  </style>
</head>
<body>
  <main>
    <h1>GAIA → CUBE Rich Notification 변환 미리보기</h1>
    <p class="lead">외부 GAIA/CUBE 호출 없이, callback 서버와 같은 변환 함수를 사용해 생성한 결과입니다.</p>
    """ + "".join(cards) + """
  </main>
</body>
</html>
"""


def write_preview_files(output_directory: Path = OUTPUT_DIRECTORY) -> tuple[Path, Path]:
    """Write the JSON payload fragment and browser-readable result files."""

    examples = preview_examples()
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "gaia_to_cube_rich_notification_preview.json"
    html_path = output_directory / "gaia_to_cube_rich_notification_preview.html"
    json_path.write_text(
        json.dumps({"examples": examples}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    html_path.write_text(build_preview_html(examples), encoding="utf-8")
    return json_path, html_path


if __name__ == "__main__":
    preview_json, preview_html = write_preview_files()
    print(f"JSON 미리보기: {preview_json}")
    print(f"HTML 미리보기: {preview_html}")
