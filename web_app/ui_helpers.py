from __future__ import annotations

import html
import json
import re
from typing import Any

import pandas as pd


TABLE_MIN_HEIGHT = 82
TABLE_MAX_HEIGHT = 460
TABLE_HEADER_HEIGHT = 34
TABLE_ROW_HEIGHT = 32
TABLE_VERTICAL_PADDING = 12
TABLE_AUTO_HEIGHT_ROWS = 8


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def compact_json_html(value: Any) -> str:
    raw = html.escape(json_text(value))
    raw = raw.replace("&quot;", '"')
    return (
        raw.replace("null", '<span class="compact-json-null">null</span>')
        .replace("true", '<span class="compact-json-boolean">true</span>')
        .replace("false", '<span class="compact-json-boolean">false</span>')
    )


def safe_markdown_text(value: Any) -> str:
    text = str(value or "")
    # Langflow/API 응답에 포함된 ~ 문자가 Markdown 취소선으로 해석되지 않게 막습니다.
    return re.sub(r"(?<!\\)~", r"\\~", text)


def format_answer_markdown_text(value: Any) -> str:
    text = _separate_answer_summary_blocks(str(value or ""))
    return safe_markdown_text(text)


def _separate_answer_summary_blocks(text: str) -> str:
    if not text.strip():
        return text
    text = _break_adjacent_metric_pairs(text)
    text = re.sub(r"(?<!\n)\s+(위 결과는|이 결과는|해당 결과는|참고:|사용 데이터셋:|적용 필터:)", r"\n\n\1", text)
    return text


def _break_adjacent_metric_pairs(text: str) -> str:
    metric_pair = r"[^\s:]{1,40}\s*:\s*[-+]?\d[\d,]*(?:\.\d+)?(?:\s*(?:%|개|건|대|Lot|Wafer|EA|WIP))?"
    pattern = re.compile(rf"({metric_pair})(?=[ \t]+{metric_pair})")
    previous = None
    result = text
    while previous != result:
        previous = result
        result = pattern.sub(r"\1  \n", result)
    return re.sub(r"\n[ \t]+(?=[^\s:]{1,40}\s*:)", "\n", result)


def chat_table_visible_rows(max_height: int = TABLE_MAX_HEIGHT) -> int:
    usable_height = max_height - TABLE_HEADER_HEIGHT - TABLE_VERTICAL_PADDING
    return max(1, usable_height // TABLE_ROW_HEIGHT)


def chat_table_height(row_count: int, max_height: int = TABLE_MAX_HEIGHT) -> int:
    clean_count = max(0, int(row_count or 0))
    if clean_count <= 0:
        return TABLE_MIN_HEIGHT
    visible_rows = chat_table_visible_rows(max_height)
    if clean_count > visible_rows:
        return max_height
    content_height = TABLE_HEADER_HEIGHT + TABLE_VERTICAL_PADDING + clean_count * TABLE_ROW_HEIGHT
    return max(TABLE_MIN_HEIGHT, min(max_height, content_height))


def chat_dataframe_height(row_count: int, max_height: int = TABLE_MAX_HEIGHT) -> str | int:
    clean_count = max(0, int(row_count or 0))
    if 0 < clean_count <= TABLE_AUTO_HEIGHT_ROWS:
        return "auto"
    return chat_table_height(clean_count, max_height)


def display_table_frame(
    frame: pd.DataFrame,
    number_mode: str = "auto_k",
    column_labels: dict[str, Any] | None = None,
    display_columns: list[Any] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    for column in result.columns:
        if not _looks_numeric_series(result[column]):
            continue
        result[column] = result[column].map(lambda value: _format_number(value, number_mode))
    result = _order_display_columns(result, display_columns)
    labels = {str(key): str(value) for key, value in (column_labels or {}).items() if str(key or "").strip() and str(value or "").strip()}
    if labels:
        result = result.rename(columns={column: labels.get(str(column), str(column)) for column in result.columns})
    return result


def _order_display_columns(frame: pd.DataFrame, display_columns: list[Any] | None = None) -> pd.DataFrame:
    priority = [str(column) for column in (display_columns or []) if str(column or "").strip()]
    ordered = [column for column in priority if column in frame.columns]
    ordered.extend(column for column in frame.columns if column not in ordered)
    return frame[ordered] if ordered else frame


def _looks_numeric_series(series: pd.Series) -> bool:
    for value in series.dropna().head(20):
        if isinstance(value, bool) or isinstance(value, str):
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if number != number:
            continue
        return True
    return False


def _format_number(value: Any, mode: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    try:
        number = float(value)
    except Exception:
        return value
    if number != number:
        return value
    if mode in {"auto_k", "k"} and abs(number) >= 10000:
        k_value = number / 1000
        return f"{int(k_value):,}K" if float(k_value).is_integer() else f"{k_value:,.1f}K"
    if float(number).is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"
