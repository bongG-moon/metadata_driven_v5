from __future__ import annotations

from markdown_rich_notification import (
    CUBE_MAX_RENDERED_ROWS,
    CUBE_MAX_SOURCE_CHARACTERS,
    CUBE_MAX_TABLE_COLUMNS,
    CUBE_TRUNCATED_TABLE_CELL,
    CUBE_TRUNCATION_MESSAGE,
    render_markdown_to_cube_body,
)


def _columns(body: dict) -> list[dict]:
    return [column for row in body["row"] for column in row["column"]]


def _labels(body: dict) -> list[str]:
    return [
        column["control"]["text"][0]
        for column in _columns(body)
        if column["type"] == "label"
    ]


def test_groups_normal_markdown_lines_into_one_production_label_row() -> None:
    body = render_markdown_to_cube_body(
        "### 조회 가능한 데이터\n\n"
        "- 총 12개 데이터셋이 등록되어 있습니다.\n"
        "- 연결 방식: Oracle 11개, Goodocs 1개"
    )

    assert body["bodystyle"] == "grid"
    assert len(body["row"]) == 1
    assert body["row"][0]["align"] == "left"
    assert body["row"][0]["width"] == "100%"
    assert _labels(body) == [
        "조회 가능한 데이터\n\n- 총 12개 데이터셋이 등록되어 있습니다.\n"
        "- 연결 방식: Oracle 11개, Goodocs 1개"
    ]


def test_builds_table_with_production_row_shape_and_proportional_widths() -> None:
    body = render_markdown_to_cube_body(
        "| 데이터셋 | 연결 방식 | 필수 조건 |\n"
        "| --- | --- | --- |\n"
        "| Equipment UPH | Oracle | 없음 |\n"
        "| HOLD History | Oracle | LOT_ID |"
    )

    assert body["bodystyle"] == "grid"
    assert [
        [column["control"]["text"][0] for column in row["column"]]
        for row in body["row"]
    ] == [
        ["데이터셋", "연결 방식", "필수 조건"],
        ["Equipment UPH", "Oracle", "없음"],
        ["HOLD History", "Oracle", "LOT_ID"],
    ]
    assert all(row["align"] == "center" for row in body["row"])
    assert all(row["width"] == "100%" for row in body["row"])
    assert all(row["border"] == "false" for row in body["row"])
    assert all(column["border"] == "true" for column in _columns(body))
    assert [column["bgcolor"] for column in body["row"][0]["column"]] == [
        "#dbdbdb",
        "#dbdbdb",
        "#dbdbdb",
    ]
    widths = [
        float(column["width"].removesuffix("%"))
        for column in body["row"][0]["column"]
    ]
    assert round(sum(widths), 1) == 100.0
    assert len(set(widths)) > 1


def test_only_complete_header_separator_body_is_a_table() -> None:
    body = render_markdown_to_cube_body("| 헤더 | 값 |\n| --- | --- |")

    assert len(body["row"]) == 1
    assert _labels(body) == ["| 헤더 | 값 |\n| --- | --- |"]


def test_turns_safe_markdown_images_into_independent_image_rows() -> None:
    body = render_markdown_to_cube_body(
        "이미지 전\n"
        "![생산 그래프](https://example.test/reports/production.png)\n"
        "이미지 후"
    )

    assert _labels(body) == ["이미지 전", "이미지 후"]
    image_column = next(
        column for column in _columns(body) if column["type"] == "image"
    )
    assert image_column["control"] == {
        "active": "true",
        "sourceurl": "https://example.test/reports/production.png",
        "color": "",
        "width": "70%",
    }


def test_keeps_existing_safe_download_links_as_hypertext_extensions() -> None:
    body = render_markdown_to_cube_body(
        "분석 결과\n"
        "📥 [CSV 다운로드](https://example.test/reports/production.csv)\n"
        '🔗 <a href="https://example.test/reports/detail">상세 분석 화면 열기</a>'
    )

    links = [column for column in _columns(body) if column["type"] == "hypertext"]
    assert _labels(body) == ["분석 결과"]
    assert [column["control"]["text"][0] for column in links] == [
        "📥 CSV 다운로드",
        "🔗 상세 분석 화면 열기",
    ]


def test_unsafe_html_is_visible_as_text_but_never_becomes_a_control() -> None:
    body = render_markdown_to_cube_body(
        "표시 전"
        "<script>alert('숨김')</script>"
        '<a href="javascript:alert(1)">위험 링크</a>'
        "표시 후"
    )

    rendered = "\n".join(_labels(body))
    assert "위험 링크" in rendered
    assert "script" not in rendered.lower()
    assert "alert" not in rendered.lower()
    assert all(column["type"] != "hypertext" for column in _columns(body))


def test_caps_source_and_preserves_a_readable_truncation_notice() -> None:
    body = render_markdown_to_cube_body("x" * (CUBE_MAX_SOURCE_CHARACTERS + 1))

    assert body["bodystyle"] == "grid"
    assert _labels(body)[-1] == CUBE_TRUNCATION_MESSAGE
    assert len(body["row"]) <= CUBE_MAX_RENDERED_ROWS


def test_caps_wide_tables_without_changing_the_outer_send_contract() -> None:
    headers = [f"헤더 {index}" for index in range(CUBE_MAX_TABLE_COLUMNS + 1)]
    values = [f"값 {index}" for index in range(CUBE_MAX_TABLE_COLUMNS + 1)]
    body = render_markdown_to_cube_body(
        "| " + " | ".join(headers) + " |\n"
        "| " + " | ".join(["---"] * len(headers)) + " |\n"
        "| " + " | ".join(values) + " |"
    )

    assert len(body["row"][0]["column"]) == CUBE_MAX_TABLE_COLUMNS
    assert body["row"][0]["column"][-1]["control"]["text"] == [
        CUBE_TRUNCATED_TABLE_CELL
    ]
    assert body["row"][1]["column"][-1]["control"]["text"] == [
        CUBE_TRUNCATED_TABLE_CELL
    ]
