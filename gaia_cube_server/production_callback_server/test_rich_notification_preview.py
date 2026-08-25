from __future__ import annotations

from rich_notification_preview import build_preview_html, preview_examples


def test_preview_uses_the_same_extraction_and_renderer_as_the_callback_server() -> None:
    examples = preview_examples()

    dataset = next(example for example in examples if example["id"] == "dataset_list")
    body = dataset["cube_body"]
    assert "현재 등록된 조회 데이터셋은 총 3개입니다." in dataset["gaia_answer"]
    assert body["bodystyle"] == "grid"
    assert [column["type"] for column in body["row"][0]["column"]] == ["label"]
    table_header = next(row for row in body["row"] if len(row["column"]) == 4)
    assert [column["type"] for column in table_header["column"]] == [
        "label",
        "label",
        "label",
        "label",
    ]

    links = next(example for example in examples if example["id"] == "download_link")
    hypertexts = [
        column
        for row in links["cube_body"]["row"]
        for column in row["column"]
        if column["type"] == "hypertext"
    ]
    assert [column["control"]["text"][0] for column in hypertexts] == [
        "📥 CSV 다운로드",
        "🔗 상세 분석 화면 열기",
    ]

    guidance = next(example for example in examples if example["id"] == "html_guidance")
    guidance_columns = [row["column"][0] for row in guidance["cube_body"]["row"]]
    assert [column["control"]["color"] for column in guidance_columns] == [""]

    image = next(example for example in examples if example["id"] == "markdown_image")
    image_column = next(
        column
        for row in image["cube_body"]["row"]
        for column in row["column"]
        if column["type"] == "image"
    )
    assert image_column["control"]["sourceurl"] == (
        "https://example.test/reports/production-trend.png"
    )


def test_preview_html_shows_the_generated_cube_body() -> None:
    html = build_preview_html(preview_examples())

    assert "데이터셋 목록 답변" in html
    assert "GAIA → CUBE Rich Notification 변환 미리보기" in html
    assert "<table>" in html
    assert "CSV 다운로드" in html
    assert "추가 조건 필요" in html
    assert "Markdown 이미지 답변" in html
    assert "production-trend.png" in html
