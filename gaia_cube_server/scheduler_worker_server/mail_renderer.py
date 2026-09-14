"""Small, allowlisted Markdown renderer for Outlook HTML mail; no remote assets.

Supports headings, paragraphs, lists, fenced code, pipe tables and HTTP(S)
links, including safe links supplied as HTML anchors. Other HTML is escaped.
Source is not summarized
or truncated, and the MIME plain-text alternative retains the original answer.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


FONT = "font-family:'Malgun Gothic','맑은 고딕',Arial,sans-serif;"
TEXT = FONT + "font-size:14px;line-height:1.75;color:#334155;word-wrap:break-word;"
LINK = re.compile(r"\[([^\]\n]+)\]\((<?(?:[^\s()<>]|\([^\s()]*\))+>?)\)")
HTML_ANCHOR = r'''((?i:<a\b(?:[^>"']|"[^"]*"|'[^']*')*>[\s\S]*?</a\s*>))'''
TOKEN = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*|" + LINK.pattern + "|" + HTML_ANCHOR)
LINK_ICONS = r"[\U0001f9ed\U0001f4e5\U0001f552-\U0001f567\u23f0-\u23f3\u231a\u231b]\ufe0f?"


class AnchorText(HTMLParser):
    """Extract href and visible label only; never forward source attributes."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.hrefs = []
        self.label = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value or "" for key, value in attrs if key == "href")

    def handle_data(self, data):
        self.label.append(data)

    def link(self):
        return "".join(self.label), self.hrefs[0] if len(self.hrefs) == 1 else ""


def safe_url(value: str) -> str | None:
    value = value.strip("<>")
    if any(ord(char) < 33 for char in value):
        return None
    try:
        parts = urlsplit(value)
        return value if parts.scheme.lower() in ("http", "https") and parts.hostname and not parts.username and not parts.password else None
    except ValueError:
        return None


def inline(source: str, links: dict[str, str]) -> str:
    output, position = [], 0
    for match in TOKEN.finditer(source):
        code, bold, label, href, anchor = match.groups()
        prefix = source[position:match.start()]
        if anchor is not None or label is not None:
            prefix = re.sub(r"(?:" + LINK_ICONS + r"\s*)+$", "", prefix)
        output.append(html.escape(prefix))
        if anchor is not None:
            label, href = AnchorText(anchor).link()
        if code is not None:
            output.append(f'<span style="background-color:#eef2f7;color:#475569;{FONT}">{html.escape(code)}</span>')
        elif bold is not None:
            output.append(f'<strong>{html.escape(bold)}</strong>')
        else:
            label = re.sub(LINK_ICONS, "", label).strip()
            url = safe_url(href)
            if url:
                links.setdefault(url, label)
                output.append(f'<a href="{html.escape(url, quote=True)}" style="color:#4657a6;text-decoration:underline;">{html.escape(label)}</a>')
            else:
                output.append(html.escape(label))
        position = match.end()
    output.append(html.escape(source[position:]))
    return "".join(output)


def cells(line: str) -> list[str]:
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def separator(line: str) -> bool:
    return "|" in line and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells(line))


def render_answer(answer: str) -> tuple[str, dict[str, str]]:
    lines = answer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output, links = [], {}
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("```"):
            block = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            escaped = "<br>".join(html.escape(item).replace(" ", "&nbsp;") for item in block)
            output.append(f'<table role="presentation" width="100%" cellpadding="12" cellspacing="0"><tr><td bgcolor="#f3f5fa" style="{TEXT}font-size:12px;">{escaped}</td></tr></table>')
        elif index + 1 < len(lines) and "|" in line and separator(lines[index + 1]):
            rows = [cells(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(cells(lines[index]))
                index += 1
            width = max(map(len, rows))
            table = ['<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:14px 0;">']
            for number, row in enumerate(rows):
                tag = "th" if number == 0 else "td"
                bg = "#edf0f9" if number == 0 else "#ffffff" if number % 2 else "#f8f9fc"
                table.append("<tr>" + "".join(f'<{tag} align="left" valign="top" bgcolor="{bg}" style="{TEXT}font-size:13px;padding:10px 12px;border:1px solid #dde3ed;">{inline(value, links)}</{tag}>' for value in row + [""] * (width - len(row))) + "</tr>")
            output.append("".join(table) + "</table>")
            continue
        elif heading := re.match(r"^#{1,6}\s+(.+)$", line):
            output.append(f'<h2 style="{FONT}font-size:18px;line-height:1.5;color:#263454;margin:22px 0 10px;">{inline(heading[1], links)}</h2>')
        elif re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", line):
            ordered = bool(re.match(r"^\d", line))
            tag = "ol" if ordered else "ul"
            output.append(f'<{tag} style="margin:8px 0 16px;padding-left:24px;{TEXT}">')
            pattern = r"^\d+[.)]\s+(.+)$" if ordered else r"^[-*+]\s+(.+)$"
            while index < len(lines) and (item := re.match(pattern, lines[index].strip())):
                output.append(f'<li style="margin-bottom:7px;">{inline(item[1], links)}</li>')
                index += 1
            output.append(f'</{tag}>')
            continue
        else:
            output.append(f'<p style="{TEXT}margin:8px 0;">{inline(line, links)}</p>')
        index += 1
    return "".join(output) or f'<p style="{TEXT}">응답 내용이 없습니다.</p>', links


def render_mail(question: str, answer: str) -> str:
    content, _ = render_answer(str(answer or ""))
    question_html = html.escape(str(question or "")).replace("\n", "<br>")
    return f'''<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f2f4f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#f2f4f8"><tr><td align="center" style="padding:24px 12px;">
<!--[if mso]><table role="presentation" width="760" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="max-width:760px;border:1px solid #e0e5ee;">
<tr><td bgcolor="#263454" style="padding:26px 28px;{FONT}"><p style="margin:0 0 8px;font-size:12px;letter-spacing:1px;color:#c9d2f1;">PTMORE PKG AGENT</p><h1 style="margin:0;font-size:24px;line-height:1.4;color:#ffffff;">스케줄링 실행 결과</h1></td></tr>
<tr><td style="padding:24px 28px 8px;"><table role="presentation" width="100%" cellpadding="16" cellspacing="0" bgcolor="#f0f3fa"><tr><td style="{TEXT}border-left:3px solid #7c89ba;"><strong style="font-size:12px;color:#617198;">실행 질문</strong><br>{question_html}</td></tr></table></td></tr>
<tr><td style="padding:4px 28px 24px;{TEXT}">{content}</td></tr>
<tr><td bgcolor="#f8f9fc" style="padding:16px 28px;border-top:1px solid #e0e5ee;{FONT}font-size:12px;line-height:1.7;color:#778397;">등록된 스케줄에 따라 자동으로 발송된 분석 결과입니다.<br>보고서 링크는 만료 시간이 지나면 열리지 않을 수 있습니다.</td></tr></table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>'''
