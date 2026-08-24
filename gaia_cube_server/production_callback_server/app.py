"""HCP-only GAIA-CUBE callback server.

One request follows one simple path:
    CUBE callback -> GAIA API -> CUBE Rich Notification

The server intentionally keeps only the current in-memory GAIA session ID for
each user and CUBE channel. It does not add workers, databases, schedulers,
retry queues, or a second callback route.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


LOGGER = logging.getLogger("gaia_cube_callback")
CUBE_CALLBACK_PATH = "/api/v1/receiver"
HELLO_CHATBOT_SENTINEL = "!@#HelloChatBot#@!"
INTERACTION_KEYS = ("UserSelection", "SendBtn")
CUBE_REPLY_REQUEST_ID = "request_cond_change_main"
CUBE_BOT_FROMUSERNAME_COUNT = 5


class SettingsError(RuntimeError):
    """Raised when required HCP configuration is missing or malformed."""


class CallbackValidationError(ValueError):
    """Raised when a callback has no safe user, channel, or message."""


class GaiaResponseError(RuntimeError):
    """Raised when GAIA has no usable final answer."""


class ExternalApiError(RuntimeError):
    """Raised when GAIA or CUBE cannot complete an HTTP request."""


class GaiaRequestError(ExternalApiError):
    """A GAIA failure with a safe category for the CUBE fallback message."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"GAIA request failed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Settings:
    """Only values needed by the HCP callback server.

    ``GAIA_API_URL`` is the complete GAIA Agent endpoint. The server never
    appends a service ID or reconstructs the URL.
    """

    gaia_api_url: str
    gaia_auth_key: str
    cube_send_url: str
    cube_bot_id: str
    cube_bot_token: str
    cube_bot_fromusername: tuple[str, ...]
    gaia_timeout_seconds: float
    cube_timeout_seconds: float
    user_error_message: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Load HCP environment variables and an optional HCP `.env` file."""

        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value or value.startswith("PASTE_") or value.startswith("<"):
                raise SettingsError(f"{name} is required and must not be a placeholder.")
            return value

        def positive_seconds(name: str, default: float) -> float:
            raw_value = os.getenv(name, str(default)).strip()
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be a number.") from exc
            if value <= 0:
                raise SettingsError(f"{name} must be greater than zero.")
            return value

        def api_url(name: str) -> str:
            value = required(name)
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path in {"", "/"}
            ):
                raise SettingsError(
                    f"{name} must be a complete http(s) API URL including its path."
                )
            return value

        raw_names = required("CUBE_BOT_FROMUSERNAME_JSON")
        try:
            names = json.loads(raw_names)
        except json.JSONDecodeError as exc:
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must be a JSON array with five bot names."
            ) from exc
        if (
            not isinstance(names, list)
            or len(names) != CUBE_BOT_FROMUSERNAME_COUNT
            or not all(isinstance(name, str) and name.strip() for name in names)
        ):
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must contain exactly five non-empty strings."
            )
        bot_names = tuple(name.strip() for name in names)
        if any(
            name.startswith("PASTE_")
            or "YOUR_" in name.upper()
            or "<" in name
            or ">" in name
            for name in bot_names
        ):
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must contain real bot display names."
            )

        return cls(
            gaia_api_url=api_url("GAIA_API_URL"),
            gaia_auth_key=required("GAIA_AUTH_KEY"),
            cube_send_url=api_url("CUBE_SEND_URL"),
            cube_bot_id=required("CUBE_BOT_ID"),
            cube_bot_token=required("CUBE_BOT_TOKEN"),
            cube_bot_fromusername=bot_names,
            gaia_timeout_seconds=positive_seconds("GAIA_TIMEOUT_SECONDS", 10),
            cube_timeout_seconds=positive_seconds("CUBE_TIMEOUT_SECONDS", 20),
            user_error_message=os.getenv(
                "USER_ERROR_MESSAGE",
                "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ).strip()
            or "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )


@dataclass(frozen=True)
class CubeCallbackEvent:
    """The three values required for one GAIA request and CUBE reply."""

    user_id: str
    channel_id: str
    message: str


class InMemorySessionStore:
    """Reuse GAIA's session ID for the same user and CUBE channel."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, user_id: str, channel_id: str) -> str:
        key = (user_id, channel_id)
        async with self._lock:
            return self._sessions.setdefault(key, f"gc_{uuid.uuid4()}")

    async def save(self, user_id: str, channel_id: str, session_id: str) -> None:
        async with self._lock:
            self._sessions[(user_id, channel_id)] = session_id


def _text(value: Any) -> str | None:
    """Return a non-empty string, otherwise None."""

    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        return next((text for item in value if (text := _text(item))), None)
    return _text(value)


def _matching_value(
    first: str | None, second: str | None, label: str
) -> str | None:
    """Use either CUBE field, but reject a callback whose two IDs disagree."""

    if first and second and first != second:
        raise CallbackValidationError(f"CUBE {label} values do not match.")
    return first or second


def parse_cube_callback(payload: Mapping[str, Any]) -> CubeCallbackEvent | None:
    """Read a CUBE callback. None means the CUBE hello control event."""

    envelope = payload.get("richnotificationmessage")
    if not isinstance(envelope, Mapping):
        raise CallbackValidationError("richnotificationmessage is required.")

    header = _mapping(envelope.get("header"))
    process = _mapping(envelope.get("process"))
    user_id = _matching_value(
        _text(_mapping(header.get("from")).get("uniquename")),
        _text(process.get("userId")),
        "user ID",
    )
    channel_id = _matching_value(
        _first_text(_mapping(header.get("to")).get("channelid")),
        _text(process.get("channelId")),
        "channel ID",
    )
    message = _text(process.get("processdata"))

    if message == HELLO_CHATBOT_SENTINEL:
        return None
    if message is None:
        message = next(
            (_text(process.get(key)) for key in INTERACTION_KEYS if _text(process.get(key))),
            None,
        )
    if not user_id or not channel_id or not message:
        raise CallbackValidationError(
            "A CUBE user ID, channel ID, and processdata or selection value are required."
        )
    return CubeCallbackEvent(user_id=user_id, channel_id=channel_id, message=message)


def _at(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    """Read the preferred GAIA answer from the last Langflow Chat Output."""

    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise GaiaResponseError("GAIA response has no outputs list.")

    for outer in reversed(outputs):
        inner_outputs = _mapping(outer).get("outputs")
        if not isinstance(inner_outputs, list):
            continue
        for component in reversed(inner_outputs):
            component = _mapping(component)
            is_chat_output = (
                component.get("component_display_name") == "Chat Output"
                or str(component.get("component_id", "")).startswith("ChatOutput-")
            )
            if not is_chat_output:
                continue

            result = _mapping(component.get("results"))
            if _at(result, "message", "data", "error") is True:
                raise GaiaResponseError("The final GAIA Chat Output is an error.")
            answer = _text(_at(result, "gaia_response", "data", "answer"))
            if answer:
                return answer
            answer = _text(_at(result, "message", "data", "text"))
            if answer:
                return answer
            raise GaiaResponseError("The final GAIA Chat Output has no answer text.")

    raise GaiaResponseError("GAIA response has no Chat Output.")


def _returned_session_id(payload: Mapping[str, Any]) -> str | None:
    return _text(payload.get("session_id"))


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
            # Nested anchors are malformed; the unfinished one remains plain text.
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
        # A missing closing </a> is not a trustworthy interactive target.
        self._finish_anchor(completed=False)
        return self.fragments


def _html_fragments(text: str) -> list[tuple[str, str, str | None]]:
    """Parse HTML with the stdlib parser so quoted attributes stay intact."""

    parser = _RichHtmlFragmentParser()
    try:
        parser.feed(text)
        return parser.finish()
    except (AssertionError, ValueError):
        # Keep unexpected malformed text readable; downstream cleaning removes tags.
        return [("text", text, None)]


def _preserve_html_line_breaks(text: str) -> str:
    """Turn common HTML block tags into renderer rows before tags are removed.

    The later HTML parser intentionally removes markup.  Without this step,
    ``<p>첫 문단</p><p>둘째 문단</p>`` would become one joined label.
    """

    return _HTML_BLOCK_TAG_RE.sub("\n", text)


def _clean_rich_text(value: Any) -> str:
    """Flatten display text so CUBE never receives raw Markdown or HTML tags."""

    if not isinstance(value, str):
        return ""

    text = "".join(fragment[1] for fragment in _html_fragments(value))
    # A second pass also removes tags that arrived HTML-entity encoded.
    text = "".join(fragment[1] for fragment in _html_fragments(unescape(text)))
    text = unescape(text).replace("\x00", "")
    # A malformed trailing tag is not useful to the user and must not be sent as text.
    text = _INCOMPLETE_HTML_TAG_RE.sub("", text)
    text = _MARKDOWN_LINK_TEXT_RE.sub(r"\1", text)
    text = re.sub(r"!\[([^\]\n]*)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"(`{1,3})(.*?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = text.replace("\\*", "*").replace("\\_", "_").replace("\\`", "`")
    text = " ".join(text.split())
    text = text.strip()
    if len(text) > CUBE_MAX_DISPLAY_TEXT_CHARACTERS:
        return text[: CUBE_MAX_DISPLAY_TEXT_CHARACTERS - 1].rstrip() + "…"
    return text


def _safe_http_url(value: Any) -> str | None:
    """Return only complete http(s) URLs for CUBE's clickable controls."""

    if not isinstance(value, str):
        return None
    decoded_url = unescape(value)
    if decoded_url != decoded_url.strip():
        return None
    url = decoded_url
    if (
        not url
        or len(url) > CUBE_MAX_LINK_URL_CHARACTERS
        or any(character.isspace() for character in url)
        or any(ord(character) < 32 for character in url)
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
    """Classify only explicit user-facing guidance; avoid broad word guesses.

    A phrase such as ``필수 조건이 있는 데이터셋`` is ordinary information,
    while ``추가 조건 필요: 날짜를 입력해 주세요`` needs visual emphasis.
    """

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
    """Give explicit warning/error/confirmation text a readable CUBE row style."""

    kind = _guidance_kind(text)
    if kind:
        return CUBE_ROW_STYLES[kind]
    return CUBE_ROW_STYLES["heading" if heading else "normal"]


def _split_markdown_links(text: str) -> list[tuple[str, str, str | None]]:
    """Split one plain-text HTML fragment into Markdown links and labels."""

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
    """Split HTML and Markdown links into safe display text and controls."""

    fragments: list[tuple[str, str, str | None]] = []
    for kind, value, href in _html_fragments(text):
        if kind == "text":
            fragments.extend(_split_markdown_links(value))
            continue

        label = _clean_rich_text(value)
        safe_url = _safe_http_url(href)
        if safe_url and label:
            fragments.append(("link", label, safe_url))
        else:
            # An unsafe or malformed anchor remains readable but cannot be clicked.
            fragments.append(("text", label, None))
    return fragments or [("text", text, None)]


def _split_markdown_table_row(line: str) -> list[str] | None:
    """Read one GFM pipe row without inferring a table from arbitrary prose."""

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
    """Recognize only a complete pipe table with its required separator row."""

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
    """Keep bullets and download/report icons attached to the following link."""

    return bool(text) and not any(character.isalnum() for character in text)


def _join_rich_text(prefix: str, text: str) -> str:
    return " ".join(part for part in (prefix.strip(), text.strip()) if part)


def _inline_rich_rows(
    text: str, *, prefix: str = "", heading: bool = False
) -> list[dict[str, Any]]:
    """Render ordinary text and valid links as one safe CUBE row per control."""

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
    """Render a validated Markdown table as CUBE Grid rows and label columns."""

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


def render_gaia_answer_to_cube_body(message_text: str) -> dict[str, Any]:
    """Convert GAIA's Markdown/HTML answer into a deterministic CUBE body.

    Headings, paragraphs, and bullets become label rows.  Only a valid GFM
    pipe table with a separator row is rendered as a Grid, and only complete
    http(s) HTML or Markdown links become clickable hypertext controls.
    """

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
        """Append only the rows that leave space for one readable truncation note."""

        remaining = max_content_rows - len(rows)
        if remaining <= 0:
            return bool(rendered_rows)
        rows.extend(rendered_rows[:remaining])
        return len(rendered_rows) > remaining

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
                truncated = True
                break
            truncated = truncated or table_truncated
            continue

        if heading := _MARKDOWN_HEADING_RE.match(line):
            if append_rows(_inline_rich_rows(heading.group("text"), heading=True)):
                truncated = True
                break
            position += 1
            continue

        if blockquote := _MARKDOWN_BLOCKQUOTE_RE.match(line):
            if append_rows(_inline_rich_rows(blockquote.group("text"))):
                truncated = True
                break
            position += 1
            continue

        if bullet := _MARKDOWN_BULLET_RE.match(line):
            marker = bullet.group("marker")
            prefix = marker if marker[0].isdigit() else "•"
            if append_rows(_inline_rich_rows(bullet.group("text"), prefix=prefix)):
                truncated = True
                break
            position += 1
            continue

        if append_rows(_inline_rich_rows(line)):
            truncated = True
            break
        position += 1

    if not rows:
        fallback = _clean_rich_text(source) or "응답 내용을 표시할 수 없습니다."
        rows = [_rich_row([_label_column(fallback)])]
    elif truncated:
        rows.append(_rich_row([_label_column(CUBE_TRUNCATION_MESSAGE)]))

    return {"bodystyle": "Grid" if has_table else "none", "row": rows}


def build_cube_rich_notification(
    settings: Settings, receiver_id: str, channel_id: str, message_text: str
) -> dict[str, Any]:
    """Build the CUBE text payload proven to work with a populated process."""

    return {
        "richnotification": {
            "header": {
                "from": settings.cube_bot_id,
                "token": settings.cube_bot_token,
                "fromusername": list(settings.cube_bot_fromusername),
                "to": {
                    "uniquename": [receiver_id],
                    "channelid": [channel_id],
                },
            },
            "content": [
                {
                    "header": {},
                    "body": render_gaia_answer_to_cube_body(message_text),
                    # CUBE did not deliver messages when this object was empty.
                    "process": {
                        "callbacktype": "url",
                        "callbackaddress": "",
                        "processdata": "",
                        "processtype": "",
                        "summary": ["", "", "", "", ""],
                        "session": {"sessionid": "", "sequence": "1"},
                        "mandatory": [],
                        "requestid": [CUBE_REPLY_REQUEST_ID],
                    },
                }
            ],
            "result": "",
        }
    }


async def call_gaia(
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: str,
    session_id: str,
    message: str,
) -> dict[str, Any]:
    """Call the exact GAIA_API_URL supplied in the environment."""

    try:
        response = await client.post(
            settings.gaia_api_url,
            headers={
                "Content-Type": "application/json",
                "X-Gaia-Auth-Key": settings.gaia_auth_key,
                "X-Gaia-User-Id": user_id,
            },
            json={"input_value": message, "user_id": user_id, "session_id": session_id},
            timeout=settings.gaia_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        raise GaiaRequestError("timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise GaiaRequestError("http_error") from exc
    except httpx.RequestError as exc:
        raise GaiaRequestError("connection") from exc
    except ValueError as exc:
        raise GaiaRequestError("invalid_json") from exc
    if not isinstance(body, dict):
        raise GaiaRequestError("unexpected_body")
    return body


def _gaia_fallback_message(settings: Settings, error: Exception) -> str:
    """Return a useful but non-sensitive explanation for the CUBE user.

    Never expose HTTP status details, internal addresses, exception text, or
    credentials.  The configured user message remains the final guidance.
    """

    if isinstance(error, GaiaRequestError):
        causes = {
            "timeout": "주의: GAIA 응답 시간이 초과되었습니다.",
            "http_error": "오류: GAIA API가 요청을 정상 처리하지 못했습니다.",
            "connection": "오류: GAIA API 연결에 실패했습니다.",
            "invalid_json": "오류: GAIA API 응답 형식을 읽지 못했습니다.",
            "unexpected_body": "오류: GAIA API 응답 형식이 예상과 다릅니다.",
        }
        cause = causes.get(error.reason, "오류: GAIA API 요청 중 오류가 발생했습니다.")
    elif isinstance(error, GaiaResponseError):
        detail = str(error)
        if "is an error" in detail:
            cause = "오류: GAIA/Langflow가 처리 오류를 반환했습니다."
        elif "no answer" in detail or "no Chat Output" in detail:
            cause = "오류: GAIA/Langflow 응답에서 최종 답변을 찾지 못했습니다."
        else:
            cause = "오류: GAIA/Langflow 응답 형식이 예상과 다릅니다."
    else:
        cause = "오류: GAIA 요청 처리 중 오류가 발생했습니다."

    return f"{cause}\n{settings.user_error_message}"


async def send_cube_message(
    client: httpx.AsyncClient,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> None:
    """Send one GAIA answer (or the safe fallback) to CUBE."""

    try:
        response = await client.post(
            settings.cube_send_url,
            headers={"Content-Type": "application/json"},
            json=build_cube_rich_notification(
                settings, receiver_id, channel_id, message_text
            ),
            timeout=settings.cube_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalApiError("CUBE message request failed.") from exc


def create_application(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the HCP application; tests inject a mock HTTP transport."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or Settings.from_env()
        app.state.sessions = InMemorySessionStore()
        app.state.http_client = httpx.AsyncClient(transport=transport)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(
        title="happy engr",
        description="CUBE callback → GAIA → CUBE response",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/hello")
    async def say_hello() -> Response:
        return Response(content="hello world!", media_type="text/plain")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "callback_path": CUBE_CALLBACK_PATH}

    @app.post(CUBE_CALLBACK_PATH)
    async def receive_cube_callback(
        payload: dict[str, Any], request: Request
    ) -> JSONResponse:
        """Run the same full flow for a CUBE callback or a manual test POST."""

        try:
            event = parse_cube_callback(payload)
        except CallbackValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if event is None:
            return JSONResponse({"status": "ignored"})

        active_settings: Settings = request.app.state.settings
        sessions: InMemorySessionStore = request.app.state.sessions
        client: httpx.AsyncClient = request.app.state.http_client
        session_id = await sessions.get_or_create(event.user_id, event.channel_id)

        try:
            gaia_response = await call_gaia(
                client, active_settings, event.user_id, session_id, event.message
            )
            answer = extract_final_answer(gaia_response)
            if returned_session_id := _returned_session_id(gaia_response):
                await sessions.save(event.user_id, event.channel_id, returned_session_id)
        except (ExternalApiError, GaiaResponseError) as exc:
            LOGGER.warning("GAIA processing failed: %s", type(exc).__name__)
            try:
                await send_cube_message(
                    client,
                    active_settings,
                    event.user_id,
                    event.channel_id,
                    _gaia_fallback_message(active_settings, exc),
                )
            except ExternalApiError as fallback_exc:
                LOGGER.warning(
                    "CUBE fallback delivery failed: %s", type(fallback_exc).__name__
                )
                # No visible fallback reached CUBE, so report the callback failure.
                return JSONResponse(
                    status_code=502,
                    content={
                        "status": "error",
                        "message": "Unable to deliver the fallback message.",
                    },
                )

            # A fallback was handed to CUBE.  Do not return 502 here: a caller
            # that retries non-2xx callbacks could otherwise send it repeatedly.
            return JSONResponse(
                content={"status": "fallback_sent"},
            )

        try:
            await send_cube_message(
                client, active_settings, event.user_id, event.channel_id, answer
            )
        except ExternalApiError:
            LOGGER.warning("CUBE answer delivery failed.")
            return JSONResponse(
                status_code=502,
                content={"status": "error", "message": "Unable to deliver the answer."},
            )

        return JSONResponse(
            content={"status": "success", "message": "GAIA answer was sent to CUBE."}
        )

    return app


# HCP runs this exact ASGI object and fixed Uvicorn entrypoint.
application = create_application()


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
