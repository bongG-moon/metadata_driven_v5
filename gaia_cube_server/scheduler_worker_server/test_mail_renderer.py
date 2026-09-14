from html.parser import HTMLParser

from mail_delivery import build_message
from mail_renderer import render_mail, render_answer, safe_url


SAMPLE = """### 실시간 생산 분석이 완료되었습니다.
- 기준: `20260914` / 공정그룹 `AA`
- 생산실적: **정상/초과 10건(25.6%)**, Abnormal 6건

| 공정 | 생산량 | 상태 |
| --- | ---: | --- |
| AA1 | 120 | 정상 |
| AA2 | 80 | 생산부족 |

우선 확인 대상은 장비필요 1건과 교체필요 14건입니다.
[상세 HTML Report 보기](http://reports.example.test/view/123?a=1&b=2) · [HTML 다운로드](http://reports.example.test/download/123)
- 링크 만료: `2026-09-14T04:20:46.365079+00:00`
"""


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_complete_report_keeps_data_and_formats_markdown():
    result = render_mail("DA공정 실시간 생산 분석 진행해줘", SAMPLE)
    tags = Tags(result).tags
    assert sum(tag == "th" for tag, attrs in tags) == 3
    assert any(tag == "ul" for tag, attrs in tags)
    assert "###" not in result and "`20260914`" not in result
    assert "25.6%" in result and "교체필요 14건" in result
    assert "2026-09-14T04:20:46.365079+00:00" in result
    assert "관련 링크" in result and "[if mso]" in result
    assert all(attrs["href"].startswith("http") for tag, attrs in tags if tag == "a")
    assert "a=1&amp;b=2" in result


def test_untrusted_content_never_becomes_html_or_unsafe_link():
    result = render_mail('<img src=x onerror="alert(1)">',
                         '<script>alert(1)</script>\n[위험](javascript:alert)\n[링크](https://safe.test/"onclick="oops)')
    tags = Tags(result).tags
    assert not any(tag in ("script", "img", "iframe") for tag, attrs in tags)
    assert not any("onclick" in attrs for tag, attrs in tags)
    assert not any(attrs.get("href", "").startswith("javascript:") for tag, attrs in tags)
    assert "&lt;script&gt;" in result
    for url in ("javascript:alert(1)", "data:text/html,test", "//example.test", "https://u:p@example.test", "https://a.test/\n"):
        assert safe_url(url) is None


def test_code_tables_empty_and_links_with_parentheses():
    result, links = render_answer("```sql\nselect * from prod where qty < 3\n```\n\n1. 첫째\n2. 둘째\n\n[문서](https://example.test/a(b))")
    assert "qty&nbsp;&lt;&nbsp;3" in result and "<ol" in result
    assert links == {"https://example.test/a(b)": "문서"}
    assert "응답 내용이 없습니다" in render_mail("", "")
    result, _ = render_answer("|A|B|\n|---|---|\n|x\\|y|z|")
    assert "x|y" in result


def test_mime_plain_original_and_html_report_share_same_answer():
    message, envelope = build_message({"email": "a@example.test"}, {"email": "b@example.test"}, "질문", SAMPLE)
    plain, rich = message.get_payload()
    assert plain.get_content_type() == "text/plain"
    assert SAMPLE in plain.get_payload(decode=True).decode("utf-8")
    assert rich.get_content_type() == "text/html"
    assert "<th" in rich.get_payload(decode=True).decode("utf-8")
    assert envelope == ["a@example.test", "b@example.test"]
