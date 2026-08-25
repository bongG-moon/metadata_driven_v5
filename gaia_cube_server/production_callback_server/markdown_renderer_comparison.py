"""Create a no-network visual comparison of the old and current Markdown renderers.

Run from this folder:

    python markdown_renderer_comparison.py

The left side calls the former renderer directly; the right side calls the
current production parser directly.  It never calls GAIA or CUBE.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from markdown_legacy_rich_notification import render_legacy_markdown_to_cube_body
from markdown_rich_notification import render_markdown_to_cube_body


OUTPUT_PATH = (
    Path(__file__).with_name("preview_output") / "markdown_renderer_comparison.html"
)

SAMPLE_MARKDOWN = """### 오늘 생산 현황
- 총 생산량: 12,480장
- 양품률: 98.7%
주의: 수치는 잠정 집계값입니다.

| 공정 | 생산량 | 상태 |
| --- | ---: | --- |
| DA | 4,820 | 정상 |
| WB | 3,160 | 확인 필요 |
| PKG | 4,500 | 정상 |

![오늘 생산 추이](https://example.test/reports/production-trend.png)

📥 [CSV 다운로드](https://example.test/reports/production.csv)"""

def legacy_renderer_body(markdown: str) -> dict[str, Any]:
    """Return the actual former renderer result for the left-side preview."""

    return render_legacy_markdown_to_cube_body(markdown)


def _control_text(column: dict[str, Any]) -> str:
    values = column.get("control", {}).get("text", [""])
    return str(values[0]) if isinstance(values, list) and values else ""


def _render_cube_body(body: dict[str, Any]) -> str:
    """Render body rows as an explanatory browser approximation, not real CUBE UI."""

    parts: list[str] = []
    table_rows: list[dict[str, Any]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        heading = table_rows[0]["column"]
        header = "".join(
            f"<th style=\"width:{html.escape(str(column.get('width', '')))};"
            f"background:{html.escape(str(column.get('bgcolor', '#dbdbdb')))};"
            f"color:{html.escape(str(column.get('control', {}).get('color', '#000000')))}\">"
            f"{html.escape(_control_text(column))}</th>"
            for column in heading
        )
        body_rows = []
        for row in table_rows[1:]:
            body_rows.append(
                "<tr>"
                + "".join(
                    f"<td style=\"background:{html.escape(str(column.get('bgcolor', '#ffffff')))};"
                    f"color:{html.escape(str(column.get('control', {}).get('color', '#000000')))}\">"
                    f"{html.escape(_control_text(column))}</td>"
                    for column in row["column"]
                )
                + "</tr>"
            )
        parts.append(
            "<table><thead><tr>"
            + header
            + "</tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table>"
        )
        table_rows = []

    for row in body.get("row", []):
        columns = row.get("column", [])
        if len(columns) > 1:
            table_rows.append(row)
            continue
        flush_table()
        if not columns:
            continue
        column = columns[0]
        kind = column.get("type")
        if kind == "image":
            source = html.escape(str(column["control"].get("sourceurl", "")))
            parts.append(
                '<div class="image-placeholder"><span>▧ 이미지 행</span>'
                f"<small>{source}</small></div>"
            )
            continue
        text = html.escape(_control_text(column))
        if kind == "hypertext":
            url = html.escape(str(column["control"].get("linkurl", "")), quote=True)
            parts.append(f'<p class="hypertext"><a href="{url}">{text}</a></p>')
            continue
        parts.append(
            '<p class="label" '
            f'style="background:{html.escape(str(column.get("bgcolor", "#fff")))};'
            f'color:{html.escape(str(column["control"].get("color", "#000")))};'
            f'border:{"1px solid #d0d7de" if str(column.get("border")).lower() == "true" else "0"}">'
            f"{text}</p>"
        )
    flush_table()
    return "".join(parts)


def build_comparison_html(markdown: str = SAMPLE_MARKDOWN) -> str:
    legacy_body = legacy_renderer_body(markdown)
    current_body = render_markdown_to_cube_body(markdown)
    source = html.escape(markdown)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GAIA Markdown 변환 방식 비교</title>
  <style>
    body {{ margin:0; background:#f5f7fa; color:#17202a; font-family:Segoe UI, Malgun Gothic, sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:34px 20px 60px; }}
    h1 {{ margin:0 0 8px; }} .lead {{ color:#52616b; }}
    .source, details {{ background:#fff; border:1px solid #dbe3ea; border-radius:10px; padding:16px; margin:20px 0; }}
    pre {{ margin:0; white-space:pre-wrap; line-height:1.55; font-family:Consolas, monospace; }}
    .comparison {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; }}
    .card {{ background:#fff; border:1px solid #dbe3ea; border-radius:12px; overflow:hidden; }}
    .card h2 {{ margin:0; padding:16px 18px; font-size:18px; }} .old h2 {{ background:#fff3e6; color:#8a4b00; }} .new h2 {{ background:#eaf4ff; color:#145a96; }}
    .meta {{ padding:0 18px; color:#52616b; font-size:13px; }} .cube {{ margin:16px; padding:8px 14px; border:1px solid #ccd6df; border-radius:8px; }}
    .label, .hypertext {{ margin:0; padding:10px 4px; white-space:pre-wrap; line-height:1.45; border-bottom:1px solid #edf1f4; }}
    .hypertext a {{ color:#1264a3; font-weight:600; }} table {{ width:100%; border-collapse:collapse; margin:10px 0; table-layout:fixed; }} th,td {{ border:1px solid #bac6d1; padding:8px; text-align:left; vertical-align:top; word-break:break-word; }} th {{ background:#dbdbdb; }}
    .image-placeholder {{ margin:12px 0; padding:22px 12px; text-align:center; border:1px dashed #8eabc4; border-radius:6px; background:#f0f7fc; color:#175d89; }} .image-placeholder small {{ display:block; margin-top:8px; color:#52616b; overflow-wrap:anywhere; }}
    .summary {{ margin-top:24px; background:#fff; border:1px solid #dbe3ea; border-radius:12px; padding:18px; }} li {{ margin:7px 0; }}
    @media (max-width:800px) {{ .comparison {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1>같은 GAIA Markdown의 CUBE 변환 방식 비교</h1>
    <p class="lead">왼쪽은 이전 변환기의 표시 규칙을 비교용으로 재현한 화면이고, 오른쪽은 현재 <code>markdown_rich_notification.py</code>가 생성한 body입니다. 실제 CUBE 앱 화면은 다를 수 있습니다.</p>
    <section class="source"><strong>입력 Markdown</strong><pre>{source}</pre></section>
    <section class="comparison">
      <article class="card old"><h2>기존 방식</h2><p class="meta">문장별 행 분리 · 경고 색상 · 동일 표 열 폭 · 이미지는 설명 텍스트</p><div class="cube">{_render_cube_body(legacy_body)}</div></article>
      <article class="card new"><h2>현재 운영 parser 방식</h2><p class="meta">일반 문장 묶음 · 내용 길이 기반 표 폭 · 실제 image 행 · 안전 링크 유지</p><div class="cube">{_render_cube_body(current_body)}</div></article>
    </section>
    <section class="summary"><strong>눈여겨볼 차이</strong><ul>
      <li>현재 방식은 제목·불릿·주의 문장을 하나의 일반 label 블록으로 묶습니다.</li>
      <li>현재 표는 긴 열에 더 넓은 폭을 주며, 헤더는 <code>#dbdbdb</code>입니다.</li>
      <li>현재 방식은 Markdown 이미지를 CUBE <code>image</code> 행으로 보냅니다.</li>
      <li>다운로드 링크와 이모지는 두 방식 모두 안전한 경우 클릭 가능한 hypertext로 유지합니다.</li>
    </ul></section>
    <details><summary>기존 방식 body JSON (비교용 재현)</summary><pre>{html.escape(json.dumps(legacy_body, ensure_ascii=False, indent=2))}</pre></details>
    <details><summary>현재 방식 body JSON (실제 생성 결과)</summary><pre>{html.escape(json.dumps(current_body, ensure_ascii=False, indent=2))}</pre></details>
  </main>
</body>
</html>"""


def write_comparison_html(output_path: Path = OUTPUT_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_comparison_html(), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    print(f"HTML 비교 파일: {write_comparison_html()}")
