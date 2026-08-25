from __future__ import annotations

from markdown_renderer_comparison import (
    SAMPLE_MARKDOWN,
    build_comparison_html,
    legacy_renderer_body,
)


def test_comparison_keeps_the_old_and_current_differences_visible() -> None:
    legacy = legacy_renderer_body(SAMPLE_MARKDOWN)
    html = build_comparison_html()

    assert legacy["bodystyle"] == "Grid"
    assert any(
        column["type"] == "label" and column["control"]["text"] == ["오늘 생산 추이"]
        for row in legacy["row"]
        for column in row["column"]
    )
    assert "기존 방식" in html
    assert "현재 운영 parser 방식" in html
    assert "내용 길이 기반 표 폭" in html
    assert "이미지 행" in html
    # The preview applies each renderer's actual table-header colour instead
    # of flattening both sides into one CSS colour.
    assert "background:#f2f2f2" in html
    assert "background:#dbdbdb" in html
