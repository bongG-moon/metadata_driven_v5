"""GAIA Markdown answer -> CUBE Rich Notification body converter.

This module follows the parser/builder shape used by the supplied production
server: normal prose is grouped into label rows, Markdown pipe tables become
grid rows with calculated column widths, and Markdown images become image rows.

It deliberately does *not* build the outer ``richnotification`` payload.  The
callback server keeps ownership of the bot header, ``process`` object, and the
actual CUBE HTTP request.  Safe ``hypertext`` handling is retained as a small
compatibility extension so existing download/report links do not regress.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse


CUBE_MAX_SOURCE_CHARACTERS = 100_000
CUBE_MAX_RENDERED_ROWS = 200
CUBE_MAX_TABLE_COLUMNS = 12
CUBE_MAX_DISPLAY_TEXT_CHARACTERS = 1_000
CUBE_MAX_LINK_URL_CHARACTERS = 4_096
CUBE_TRUNCATION_MESSAGE = "일부 응답은 CUBE 표시 한도로 생략되었습니다."
CUBE_TRUNCATED_TABLE_CELL = "…"

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)(?:\s+#+\s*)?$")
_BLOCKQUOTE_RE = re.compile(r"^\s*(?P<markers>>+)\s?(?P<text>.*)$")
_HORIZONTAL_RULE_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!\!)\[(?P<label>[^\]\n]+)\]\((?P<href>[^\s)]+)\)"
)
_MARKDOWN_LINK_TEXT_RE = re.compile(r"!?\[([^\]\n]*)\]\([^\n)]*\)")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\n]*\]\(\s*(?P<url><[^>\s]+>|[^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_INCOMPLETE_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]*$")
_MARKDOWN_CODE_RE = re.compile(r"(`{1,3})(.*?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_STRIKE_RE = re.compile(r"~~(.*?)~~")
_ITALIC_ASTERISK_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")


class _VisibleHtmlParser(HTMLParser):
    """Extract visible HTML text and complete anchors without executable content."""

    _BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}

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
            _, previous_text, previous_url = self.fragments[-1]
            self.fragments[-1] = ("text", previous_text + text, previous_url)
            return
        self.fragments.append((kind, text, url))

    def _finish_anchor(self, *, completed: bool) -> None:
        if not self._anchor_open:
            return
        self._append(
            "anchor" if completed else "text",
            "".join(self._anchor_text),
            self._anchor_href,
        )
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
        elif tag == "br":
            self._append("text", "\n")
        elif tag in self._BLOCK_TAGS:
            self._append("text", "\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "br" and not self._ignored_depth:
            self._append("text", "\n")

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
        elif tag in self._BLOCK_TAGS:
            self._append("text", "\n")

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


def _html_fragments(value: str) -> list[tuple[str, str, str | None]]:
    parser = _VisibleHtmlParser()
    try:
        parser.feed(value)
        return parser.finish()
    except (AssertionError, ValueError):
        # A malformed answer still needs to be sent as readable text.
        return [("text", value, None)]


def _safe_http_url(value: Any) -> str | None:
    """Allow only complete, non-credentialed http(s) URLs in CUBE controls."""

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


def _clean_inline_markup(value: str) -> str:
    """Remove display-only Markdown markers while preserving ordinary prose."""

    text = _MARKDOWN_LINK_TEXT_RE.sub(r"\1", value)
    text = re.sub(r"!\[([^\]\n]*)\]\([^\n)]*\)", r"\1", text)
    text = _MARKDOWN_CODE_RE.sub(r"\2", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_ASTERISK_RE.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"\1", text)
    return (
        text.replace(r"\|", "|")
        .replace(r"\*", "*")
        .replace(r"\_", "_")
        .replace(r"\`", "`")
    )


def clean_display_text(value: Any) -> str:
    """Return bounded visible text, retaining intentional line breaks."""

    if not isinstance(value, str):
        return ""
    text = "".join(fragment[1] for fragment in _html_fragments(value))
    text = "".join(fragment[1] for fragment in _html_fragments(unescape(text)))
    text = _INCOMPLETE_HTML_TAG_RE.sub("", unescape(text).replace("\x00", ""))
    text = _clean_inline_markup(text)
    # Horizontal whitespace is noise in CUBE; newlines mark the grouped text block.
    text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
    if len(text) > CUBE_MAX_DISPLAY_TEXT_CHARACTERS:
        return text[: CUBE_MAX_DISPLAY_TEXT_CHARACTERS - 1].rstrip() + "…"
    return text


def normalize_text_line(line: str) -> str:
    """Apply the supplied production parser's line-level normalization."""

    text = line.rstrip("\n")
    if _HORIZONTAL_RULE_RE.match(text):
        return "―" * 40
    if heading := _HEADING_RE.match(text):
        text = heading.group("text")
    elif quote := _BLOCKQUOTE_RE.match(text):
        depth = len(quote.group("markers"))
        text = (" " * max(depth - 1, 0)) + quote.group("text")
    # Keep links/HTML intact until _make_text_rows can turn safe ones into
    # native hypertext controls. Cleaning here would erase their URL first.
    return text


def _make_table_cell(text: str, width: str, *, header: bool) -> dict[str, Any]:
    return {
        "bgcolor": "#dbdbdb" if header else "#ffffff",
        "border": "true",
        "align": "center",
        "valign": "middle",
        "width": width,
        "type": "label",
        "control": {"active": "true", "text": [text], "color": ""},
    }


def _make_table_row(columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "bgcolor": "#ffffff",
        "border": "false",
        "align": "center",
        "width": "100%",
        "column": columns,
    }


def _make_text_label(text: str) -> dict[str, Any]:
    return {
        "bgcolor": "#ffffff",
        "border": "false",
        "align": "left",
        "width": "100%",
        "column": [
            {
                "bgcolor": "#ffffff",
                "border": "false",
                "align": "left",
                "valign": "middle",
                "width": "100%",
                "type": "label",
                "control": {"active": "true", "text": [text], "color": ""},
            }
        ],
    }


def _make_image_row(url: str) -> dict[str, Any]:
    return {
        "bgcolor": "#ffffff",
        "border": "false",
        "align": "left",
        "width": "100%",
        "column": [
            {
                "bgcolor": "#ffffff",
                "border": "false",
                "align": "center",
                "valign": "middle",
                "width": "100%",
                "type": "image",
                "control": {
                    "active": "true",
                    "sourceurl": url,
                    "color": "",
                    "width": "70%",
                },
            }
        ],
    }


def _make_hypertext_row(text: str, url: str) -> dict[str, Any]:
    """Keep previously working download/report links as native controls."""

    return {
        "bgcolor": "#ffffff",
        "border": "false",
        "align": "left",
        "width": "100%",
        "column": [
            {
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
        ],
    }


def _split_markdown_links(text: str) -> list[tuple[str, str, str | None]]:
    fragments: list[tuple[str, str, str | None]] = []
    position = 0
    for match in _MARKDOWN_LINK_RE.finditer(text):
        if match.start() > position:
            fragments.append(("text", text[position : match.start()], None))
        label = clean_display_text(match.group("label"))
        safe_url = _safe_http_url(match.group("href"))
        fragments.append(("link" if safe_url and label else "text", label, safe_url))
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
        label = clean_display_text(value)
        safe_url = _safe_http_url(href)
        fragments.append(("link" if safe_url and label else "text", label, safe_url))
    return fragments or [("text", text, None)]


def _is_link_decoration(text: str) -> bool:
    return bool(text) and not any(character.isalnum() for character in text)


def _split_trailing_link_decoration(text: str) -> tuple[str, str]:
    """Separate a final emoji/bullet line so it stays attached to its link."""

    lines = text.splitlines()
    if not lines:
        return text, ""
    final = lines[-1].strip()
    if not _is_link_decoration(final):
        return text, ""
    return "\n".join(lines[:-1]).strip(), final


def _join_link_text(prefix: str, text: str) -> str:
    return " ".join(part for part in (prefix.strip(), text.strip()) if part)


def _make_text_rows(text: str) -> list[dict[str, Any]]:
    """Produce grouped labels, retaining safe links as prior native hypertext."""

    rows: list[dict[str, Any]] = []
    pending_prefix = ""
    fragments = _split_rich_links(text)
    for index, (kind, value, url) in enumerate(fragments):
        if kind == "link" and url:
            label = _join_link_text(pending_prefix, clean_display_text(value))
            pending_prefix = ""
            if label:
                rows.append(_make_hypertext_row(label, url))
            continue

        display_text = clean_display_text(value)
        if not display_text:
            continue
        next_is_link = index + 1 < len(fragments) and fragments[index + 1][0] == "link"
        if next_is_link:
            before, decoration = _split_trailing_link_decoration(display_text)
            if before:
                rows.append(_make_text_label(_join_link_text(pending_prefix, before)))
                pending_prefix = ""
            if decoration:
                pending_prefix = _join_link_text(pending_prefix, decoration)
                continue
            if _is_link_decoration(display_text):
                pending_prefix = _join_link_text(pending_prefix, display_text)
                continue

        label = _join_link_text(pending_prefix, display_text)
        pending_prefix = ""
        if label:
            rows.append(_make_text_label(label))

    if pending_prefix:
        rows.append(_make_text_label(pending_prefix))
    return rows


def _split_cells(row: str) -> list[str]:
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", text)]


def _is_table_row(line: str) -> bool:
    candidate = line.strip()
    return candidate.startswith("|") and candidate.endswith("|") and candidate.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    if not _is_table_row(line):
        return False
    cells = _split_cells(line)
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def _calc_widths(headers: list[str], body_rows: list[list[str]]) -> list[str]:
    """Use the supplied production parser's content-length-based width policy."""

    representative = [len(cell.replace("\n", " ")) for cell in headers]
    for row in body_rows:
        for index, cell in enumerate(row[: len(representative)]):
            representative[index] = max(
                representative[index], len(cell.replace("\n", " "))
            )
    total = sum(representative)
    if total <= 0:
        return [f"{round(100 / len(headers), 1)}%" for _ in headers]
    raw = [max(10.0, min(70.0, value / total * 100)) for value in representative]
    normalized = [round(value / sum(raw) * 100, 1) for value in raw]
    normalized[-1] = round(normalized[-1] + 100 - sum(normalized), 1)
    return [f"{value}%" for value in normalized]


def _parse_table(header_line: str, body_lines: Iterable[str]) -> list[dict[str, Any]]:
    header = _split_cells(header_line)
    body_rows = [_split_cells(line) for line in body_lines]
    if not header or not body_rows:
        return []
    if len(header) > CUBE_MAX_TABLE_COLUMNS:
        header = header[: CUBE_MAX_TABLE_COLUMNS - 1] + [CUBE_TRUNCATED_TABLE_CELL]
        body_rows = [
            row[: CUBE_MAX_TABLE_COLUMNS - 1] + [CUBE_TRUNCATED_TABLE_CELL]
            for row in body_rows
        ]
    column_count = len(header)
    normalized_rows: list[list[str]] = []
    for row in body_rows:
        row = (row + [""] * column_count)[:column_count]
        normalized_rows.append([clean_display_text(cell) or "-" for cell in row])
    clean_header = [clean_display_text(cell) or "-" for cell in header]
    widths = _calc_widths(clean_header, normalized_rows)
    rendered = [
        _make_table_row(
            [
                _make_table_cell(cell, widths[index], header=True)
                for index, cell in enumerate(clean_header)
            ]
        )
    ]
    rendered.extend(
        _make_table_row(
            [
                _make_table_cell(cell, widths[index], header=False)
                for index, cell in enumerate(row)
            ]
        )
        for row in normalized_rows
    )
    return rendered


def _image_urls(line: str) -> list[str]:
    urls: list[str] = []
    for match in _MARKDOWN_IMAGE_RE.finditer(line):
        raw_url = match.group("url")
        urls.append(raw_url[1:-1] if raw_url.startswith("<") else raw_url)
    return urls


def render_markdown_to_cube_body(message_text: Any) -> dict[str, Any]:
    """Create the supplied production parser's CUBE ``body`` object.

    The body is always ``grid`` because the live production parser uses the
    same body style for prose, table, and image rows.  This function has no
    knowledge of bot credentials, recipients, ``process``, or HTTP sending.
    """

    source = message_text if isinstance(message_text, str) else str(message_text or "")
    source_truncated = len(source) > CUBE_MAX_SOURCE_CHARACTERS
    if source_truncated:
        source = source[:CUBE_MAX_SOURCE_CHARACTERS]
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict[str, Any]] = []
    truncated = source_truncated
    index = 0
    maximum_content_rows = CUBE_MAX_RENDERED_ROWS - 1

    def append_rows(new_rows: list[dict[str, Any]]) -> bool:
        nonlocal truncated
        remaining = maximum_content_rows - len(rows)
        if remaining <= 0:
            truncated = truncated or bool(new_rows)
            return bool(new_rows)
        rows.extend(new_rows[:remaining])
        was_cut = len(new_rows) > remaining
        truncated = truncated or was_cut
        return was_cut

    while index < len(lines):
        if len(rows) >= maximum_content_rows:
            truncated = True
            break
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        if index + 1 < len(lines) and _is_table_row(line) and _is_table_separator(
            lines[index + 1]
        ):
            body_lines: list[str] = []
            position = index + 2
            while position < len(lines) and _is_table_row(lines[position]):
                body_lines.append(lines[position])
                position += 1
            table_rows = _parse_table(line, body_lines)
            if table_rows:
                append_rows(table_rows)
                index = position
                continue

        urls = _image_urls(line)
        if urls:
            safe_urls = [url for url in urls if _safe_http_url(url)]
            if safe_urls:
                append_rows([_make_image_row(url) for url in safe_urls])
            else:
                append_rows(_make_text_rows(normalize_text_line(line)))
            index += 1
            continue

        text_block: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            is_table_start = (
                index + 2 < len(lines)
                and _is_table_row(candidate)
                and _is_table_separator(lines[index + 1])
                and _is_table_row(lines[index + 2])
            )
            if is_table_start or _image_urls(candidate):
                break
            text_block.append(normalize_text_line(candidate))
            index += 1
        text = "\n".join(text_block).strip()
        if text:
            append_rows(_make_text_rows(text))

    if not rows:
        rows = [_make_text_label("응답 내용을 표시할 수 없습니다.")]
    elif truncated:
        rows.append(_make_text_label(CUBE_TRUNCATION_MESSAGE))
    return {"bodystyle": "grid", "row": rows}
