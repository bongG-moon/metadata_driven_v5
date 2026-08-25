"""Former GAIA Markdown -> CUBE Rich Notification body renderer.

This is the renderer that was previously embedded in ``app.py``.  It is kept
as a separate, runnable case so the CUBE callback server can be tested with
the former visual rules without changing CUBE header, process, session, or
GAIA integration code.

Its visible behaviour differs from ``markdown_rich_notification.py``:

* headings, bullets, and ordinary lines are separate label rows;
* explicit warning/error/confirmation text receives a coloured label row;
* table columns have equal widths; and
* Markdown images become their visible alt text, not a CUBE ``image`` row.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse


_MARKDOWN_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+(?P<text>.*?)(?:\s+#+\s*)?$"
)
_MARKDOWN_BULLET_RE = re.compile(
    r"^\s*(?P<marker>[-*+•]|\d+[.)])\s+(?P<text>.+?)\s*$"
)
_MARKDOWN_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(?P<text>.+?)\s*$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_INCOMPLETE_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]*$")
_HTML_BLOCK_TAG_RE = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|h[1-6]|blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)
_MARKDOWN_LINK_TEXT_RE = re.compile(r"!?\[([^\]\n]*)\]\([^\n)]*\)")
_MARKDOWN_RICH_LINK_RE = re.compile(
    r"(?<!\!)\[(?P<label>[^\]\n]+)\]\((?P<href>[^\s)]+)\)"
)

CUBE_MAX_SOURCE_CHARACTERS = 100_000
CUBE_MAX_RENDERED_ROWS = 200
CUBE_MAX_TABLE_COLUMNS = 12
CUBE_MAX_DISPLAY_TEXT_CHARACTERS = 1_000
CUBE_MAX_LINK_URL_CHARACTERS = 4_096
CUBE_TRUNCATION_MESSAGE = "일부 응답은 CUBE 표시 한도로 생략되었습니다."
CUBE_TRUNCATED_TABLE_CELL = "…"
CUBE_ROW_STYLES = {
    "normal": {"bgcolor": "#ffffff", "border": "false", "color": "#000000"},
    "heading": {"bgcolor": "#ffffff", "border": "false", "color": "#1f4e79"},
    "warning": {"bgcolor": "#fff8e6", "border": "true", "color": "#9a6700"},
    "error": {"bgcolor": "#fff1f1", "border": "true", "color": "#b42318"},
    "confirmation": {"bgcolor": "#eef6ff", "border": "true", "color": "#1f4e79"},
}


class _RichHtmlFragmentParser(HTMLParser):
    """Keep visible text and complete anchors while discarding executable HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[tuple[str, str, str | None]] = []
        self._ignored_depth = 0
        self._anchor_open = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def _append(self, kind: str, text: str, url: str | None = None) -> None:
        if not text:
            return
        if kind == "text" and self.fragments and self.fragments[-1][0] == "text":
            previous_kind, previous_text, previous_url = self.fragments[-1]
            self.fragments[-1] = (previous_kind, previous_text + text, previous_url)
            return
        self.fragments.append((kind, text, url))

    def _finish_anchor(self, *, completed: bool) -> None:
        if not self._anchor_open:
            return
        text = "".join(self._anchor_text)
        self._append("anchor" if completed else "text", text, self._anchor_href)
        self._anchor_open = False
        self._anchor_href = None
        self._anchor_text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "a":
            self._finish_anchor(completed=False)
            self._anchor_open = True
            self._anchor_href = next(
                (value for name, value in attrs if name.lower() == "href"), None
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a":
            self._finish_anchor(completed=True)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._anchor_open:
            self._anchor_text.append(data)
        else:
            self._append("text", data)

    def finish(self) -> list[tuple[str, str, str | None]]:
        self.close()
        self._finish_anchor(completed=False)
        return self.fragments


def _html_fragments(text: str) -> list[tuple[str, str, str | None]]:
    parser = _RichHtmlFragmentParser()
    try:
        parser.feed(text)
        return parser.finish()
    except (AssertionError, ValueError):
        return [("text", text, None)]


def _preserve_html_line_breaks(text: str) -> str:
    return _HTML_BLOCK_TAG_RE.sub("\n", text)


def _clean_rich_text(value: Any) -> str:
    """Flatten display text so CUBE never receives raw Markdown or HTML tags."""

    if not isinstance(value, str):
        return ""
    text = "".join(fragment[1] for fragment in _html_fragments(value))
    text = "".join(fragment[1] for fragment in _html_fragments(unescape(text)))
    text = unescape(text).replace("\x00", "")
    text = _INCOMPLETE_HTML_TAG_RE.sub("", text)
    text = _MARKDOWN_LINK_TEXT_RE.sub(r"\1", text)
    text = re.sub(r"!\[([^\]\n]*)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"(`{1,3})(.*?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = text.replace("\\*", "*").replace("\\_", "_").replace("\\`", "`")
    text = " ".join(text.split()).strip()
    if len(text) > CUBE_MAX_DISPLAY_TEXT_CHARACTERS:
        return text[: CUBE_MAX_DISPLAY_TEXT_CHARACTERS - 1].rstrip() + "…"
    return text


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = unescape(value)
    if (
        url != url.strip()
        or not url
        or len(url) > CUBE_MAX_LINK_URL_CHARACTERS
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url


def _guidance_kind(text: str) -> str | None:
    """Classify only explicit messages that need a visually distinct row."""

    normalized = _clean_rich_text(text)
    normalized = re.sub(r"^(?:[•>*-]+\s*)+", "", normalized)
    normalized = re.sub(r"^(?:[⚠❗❌ℹ️]+\s*)+", "", normalized).strip()
    if re.match(
        r"^(?:(?:오류|에러|실패)(?=$|\s|[:：]|[가이은는]|했|하)|(?:처리|조회|요청|실행|연결)\s*(?:불가|실패|오류))",
        normalized,
    ):
        return "error"
    if re.match(r"^(?:주의(?:사항)?|경고|유의)(?=$|\s|[:：]|[가이은는])", normalized):
        return "warning"
    if re.match(
        r"^(?:추가\s*(?:조건|정보)|(?:사용자\s*)?(?:확인|입력|선택|승인)|필수\s*(?:값|입력)|조건\s*입력)\s*(?:이|가|을|를)?\s*(?:필요|요청)(?:합니다|됩니다|됨|해요)?",
        normalized,
    ):
        return "confirmation"
    return None


def _label_style(text: str, *, heading: bool) -> dict[str, str]:
    kind = _guidance_kind(text)
    if kind:
        return CUBE_ROW_STYLES[kind]
    return CUBE_ROW_STYLES["heading" if heading else "normal"]


def _split_markdown_links(text: str) -> list[tuple[str, str, str | None]]:
    fragments: list[tuple[str, str, str | None]] = []
    position = 0
    for match in _MARKDOWN_RICH_LINK_RE.finditer(text):
        if match.start() > position:
            fragments.append(("text", text[position : match.start()], None))
        label = _clean_rich_text(match.group("label"))
        safe_url = _safe_http_url(match.group("href"))
        if safe_url and label:
            fragments.append(("link", label, safe_url))
        else:
            fragments.append(("text", label or _clean_rich_text(match.group(0)), None))
        position = match.end()
    if position < len(text):
        fragments.append(("text", text[position:], None))
    return fragments or [("text", text, None)]


def _split_rich_links(text: str) -> list[tuple[str, str, str | None]]:
    fragments: list[tuple[str, str, str | None]] = []
    for kind, value, href in _html_fragments(text):
        if kind == "text":
            fragments.extend(_split_markdown_links(value))
            continue
        label = _clean_rich_text(value)
        safe_url = _safe_http_url(href)
        fragments.append(("link" if safe_url and label else "text", label, safe_url))
    return fragments or [("text", text, None)]


def _split_markdown_table_row(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", row)]
    return cells if cells and any(cells) else None


def _read_markdown_table(
    lines: list[str], start: int
) -> tuple[list[str], list[list[str]], int, bool] | None:
    if start + 1 >= len(lines):
        return None
    header = _split_markdown_table_row(lines[start])
    separator = _split_markdown_table_row(lines[start + 1])
    if (
        not header
        or not separator
        or len(header) != len(separator)
        or not all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in separator)
    ):
        return None
    data_rows: list[list[str]] = []
    truncated = len(header) > CUBE_MAX_TABLE_COLUMNS
    position = start + 2
    while position < len(lines):
        row = _split_markdown_table_row(lines[position])
        if row is None or len(row) != len(header):
            break
        if len(data_rows) < CUBE_MAX_RENDERED_ROWS:
            data_rows.append(row)
        else:
            truncated = True
        position += 1
    return header, data_rows, position, truncated


def _rich_row(
    columns: list[dict[str, Any]], *, bgcolor: str = "#ffffff", border: str = "false"
) -> dict[str, Any]:
    return {
        "bgcolor": bgcolor,
        "border": border,
        "align": "",
        "width": "",
        "column": columns,
    }


def _label_column(
    text: str,
    *,
    width: str = "100%",
    bgcolor: str = "#ffffff",
    border: str = "false",
    color: str = "#000000",
) -> dict[str, Any]:
    return {
        "bgcolor": bgcolor,
        "border": border,
        "align": "left",
        "valign": "middle",
        "width": width,
        "type": "label",
        "control": {"active": "true", "text": [text], "color": color},
    }


def _hypertext_column(text: str, url: str) -> dict[str, Any]:
    return {
        "bgcolor": "#ffffff",
        "border": "false",
        "align": "left",
        "valign": "middle",
        "width": "100%",
        "type": "hypertext",
        "control": {
            "active": "true",
            "text": [text],
            "linkurl": url,
            "opengraph": "false",
        },
    }


def _is_link_decoration(text: str) -> bool:
    return bool(text) and not any(character.isalnum() for character in text)


def _join_rich_text(prefix: str, text: str) -> str:
    return " ".join(part for part in (prefix.strip(), text.strip()) if part)


def _inline_rich_rows(
    text: str, *, prefix: str = "", heading: bool = False
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending_prefix = prefix
    fragments = _split_rich_links(text)
    for index, (kind, value, url) in enumerate(fragments):
        if kind == "link" and url:
            label = _join_rich_text(pending_prefix, value)
            pending_prefix = ""
            if label:
                rows.append(_rich_row([_hypertext_column(label, url)]))
            continue
        display_text = _clean_rich_text(value)
        if not display_text:
            continue
        next_is_link = index + 1 < len(fragments) and fragments[index + 1][0] == "link"
        if next_is_link and _is_link_decoration(display_text):
            pending_prefix = _join_rich_text(pending_prefix, display_text)
            continue
        label = _join_rich_text(pending_prefix, display_text)
        pending_prefix = ""
        if label:
            style = _label_style(label, heading=heading)
            rows.append(
                _rich_row(
                    [
                        _label_column(
                            label,
                            bgcolor=style["bgcolor"],
                            border=style["border"],
                            color=style["color"],
                        )
                    ],
                    bgcolor=style["bgcolor"],
                    border=style["border"],
                )
            )
    if pending_prefix:
        style = _label_style(pending_prefix, heading=heading)
        rows.append(
            _rich_row(
                [
                    _label_column(
                        pending_prefix,
                        bgcolor=style["bgcolor"],
                        border=style["border"],
                        color=style["color"],
                    )
                ],
                bgcolor=style["bgcolor"],
                border=style["border"],
            )
        )
    return rows


def _grid_table_rows(header: list[str], data_rows: list[list[str]]) -> list[dict[str, Any]]:
    if len(header) > CUBE_MAX_TABLE_COLUMNS:
        header = header[: CUBE_MAX_TABLE_COLUMNS - 1] + [CUBE_TRUNCATED_TABLE_CELL]
        data_rows = [
            row[: CUBE_MAX_TABLE_COLUMNS - 1] + [CUBE_TRUNCATED_TABLE_CELL]
            for row in data_rows
        ]
    width = f"{100 / len(header):.6g}%"

    def grid_row(cells: list[str], *, is_header: bool) -> dict[str, Any]:
        bgcolor = "#f2f2f2" if is_header else "#ffffff"
        return _rich_row(
            [
                _label_column(
                    _clean_rich_text(cell),
                    width=width,
                    bgcolor=bgcolor,
                    border="true",
                    color="#1f4e79" if is_header else "#000000",
                )
                for cell in cells
            ],
            bgcolor=bgcolor,
            border="true",
        )

    return [grid_row(header, is_header=True)] + [
        grid_row(row, is_header=False) for row in data_rows
    ]


def render_legacy_markdown_to_cube_body(message_text: Any) -> dict[str, Any]:
    """Convert a GAIA answer using the former per-line CUBE presentation."""

    source = message_text if isinstance(message_text, str) else str(message_text or "")
    source_truncated = len(source) > CUBE_MAX_SOURCE_CHARACTERS
    if source_truncated:
        source = source[:CUBE_MAX_SOURCE_CHARACTERS]
    source = _preserve_html_line_breaks(source)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict[str, Any]] = []
    has_table = False
    truncated = source_truncated
    position = 0
    max_content_rows = CUBE_MAX_RENDERED_ROWS - 1

    def append_rows(rendered_rows: list[dict[str, Any]]) -> bool:
        nonlocal truncated
        remaining = max_content_rows - len(rows)
        if remaining <= 0:
            truncated = truncated or bool(rendered_rows)
            return bool(rendered_rows)
        rows.extend(rendered_rows[:remaining])
        was_cut = len(rendered_rows) > remaining
        truncated = truncated or was_cut
        return was_cut

    while position < len(lines):
        if len(rows) >= max_content_rows:
            truncated = True
            break
        line = lines[position]
        if not line.strip():
            position += 1
            continue
        table = _read_markdown_table(lines, position)
        if table:
            header, data_rows, position, table_truncated = table
            has_table = True
            if append_rows(_grid_table_rows(header, data_rows)):
                break
            truncated = truncated or table_truncated
            continue
        if heading := _MARKDOWN_HEADING_RE.match(line):
            if append_rows(_inline_rich_rows(heading.group("text"), heading=True)):
                break
            position += 1
            continue
        if blockquote := _MARKDOWN_BLOCKQUOTE_RE.match(line):
            if append_rows(_inline_rich_rows(blockquote.group("text"))):
                break
            position += 1
            continue
        if bullet := _MARKDOWN_BULLET_RE.match(line):
            marker = bullet.group("marker")
            prefix = marker if marker[0].isdigit() else "•"
            if append_rows(_inline_rich_rows(bullet.group("text"), prefix=prefix)):
                break
            position += 1
            continue
        if append_rows(_inline_rich_rows(line)):
            break
        position += 1

    if not rows:
        fallback = _clean_rich_text(source) or "응답 내용을 표시할 수 없습니다."
        rows = [_rich_row([_label_column(fallback)])]
    elif truncated:
        rows.append(_rich_row([_label_column(CUBE_TRUNCATION_MESSAGE)]))
    return {"bodystyle": "Grid" if has_table else "none", "row": rows}
