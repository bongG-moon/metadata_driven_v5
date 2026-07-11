from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_app.data_ref_store import DEFAULT_DATABASE, DEFAULT_RESULT_COLLECTION, load_data_ref_rows


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PREVIEW_LIMIT = 100


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve MongoDB data_ref rows as local CSV downloads.")
    parser.add_argument("--host", default=os.getenv("DATA_REF_DOWNLOAD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DATA_REF_DOWNLOAD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--env-file", default=os.getenv("DATA_REF_DOWNLOAD_ENV_FILE", str(ROOT / ".env")))
    parser.add_argument("--preview-limit", type=int, default=int(os.getenv("DATA_REF_DOWNLOAD_PREVIEW_LIMIT", str(DEFAULT_PREVIEW_LIMIT))))
    args = parser.parse_args()

    load_dotenv(args.env_file)
    config = ServerConfig(
        mongo_uri=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "",
        mongo_database=os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB_NAME") or DEFAULT_DATABASE,
        result_collection=os.getenv("MONGODB_RESULT_COLLECTION") or DEFAULT_RESULT_COLLECTION,
        preview_limit=max(0, args.preview_limit),
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
    print(f"data_ref download server: http://{args.host}:{args.port}")
    print("Langflow component setting:")
    print(f"  21 답변 메시지 어댑터.download_base_url = http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        server.server_close()
    return 0


class ServerConfig:
    def __init__(self, mongo_uri: str, mongo_database: str, result_collection: str, preview_limit: int) -> None:
        self.mongo_uri = mongo_uri
        self.mongo_database = mongo_database
        self.result_collection = result_collection
        self.preview_limit = preview_limit


def make_handler(config: ServerConfig) -> type[BaseHTTPRequestHandler]:
    class DataRefDownloadHandler(BaseHTTPRequestHandler):
        server_version = "DataRefDownloadServer/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/health", "/healthz"}:
                self.send_json({"ok": True})
                return
            if parsed.path in {"/", "/view"}:
                self.render_view(parsed.query)
                return
            if parsed.path in {"/download.csv", "/download"}:
                self.render_csv(parsed.query)
                return
            if parsed.path == "/download.json":
                self.render_json(parsed.query)
                return
            self.send_error_page(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")

        def render_view(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=config.preview_limit)
            if not resolved["ok"]:
                self.send_html(error_page("다운로드 링크 오류", resolved["message"]), status=resolved_status(resolved))
                return
            ref = resolved["ref"]
            loaded = resolved["loaded"]
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
            csv_url = "/download.csv?" + urlencode({"download_ref": encode_data_ref(ref)})
            json_url = "/download.json?" + urlencode({"download_ref": encode_data_ref(ref)})
            body = render_data_page(ref, loaded, rows, columns, csv_url, json_url, config.preview_limit)
            self.send_html(body)

        def render_csv(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=None)
            if not resolved["ok"]:
                self.send_plain(resolved["message"], status=resolved_status(resolved))
                return
            ref = resolved["ref"]
            loaded = resolved["loaded"]
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
            payload = rows_to_csv_bytes(rows, columns)
            filename = download_filename(ref, "csv")
            self.send_bytes(payload, "text/csv; charset=utf-8", filename=filename)

        def render_json(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=None)
            if not resolved["ok"]:
                self.send_json({"ok": False, "message": resolved["message"]}, status=resolved_status(resolved))
                return
            payload = {"data_ref": resolved["ref"], "loaded": resolved["loaded"]}
            self.send_json(payload, filename=download_filename(resolved["ref"], "json"))

        def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            data = body.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_plain(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = str(text).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK, filename: str = "") -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_bytes(self, payload: bytes, content_type: str, filename: str = "") -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_error_page(self, status: HTTPStatus, message: str) -> None:
            self.send_html(error_page(status.phrase, message), status=status)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}")

    return DataRefDownloadHandler


def resolve_request(query: str, config: ServerConfig, limit: int | None) -> dict[str, Any]:
    try:
        ref = data_ref_from_query(query)
    except Exception as exc:
        return {"ok": False, "message": f"download_ref 토큰 해석 실패: {exc}", "ref": {}, "loaded": {}}
    if not isinstance(ref, dict):
        return {"ok": False, "message": "download_ref 또는 ref_id가 필요합니다.", "ref": {}, "loaded": {}}
    ref_id = str(ref.get("ref_id") or "").strip()
    if not ref_id:
        return {"ok": False, "message": "data_ref.ref_id가 비어 있습니다.", "ref": ref, "loaded": {}}
    if not config.mongo_uri:
        return {"ok": False, "message": "MONGODB_URI 또는 MONGO_URI 환경값이 필요합니다.", "ref": ref, "loaded": {}}
    try:
        loaded = load_data_ref_rows(
            ref,
            config.mongo_uri,
            default_database=config.mongo_database,
            default_collection=config.result_collection,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "message": f"MongoDB data_ref 조회 실패: {exc}", "ref": ref, "loaded": {}}
    if not loaded.get("ok"):
        status = HTTPStatus.GONE if loaded.get("expired") else HTTPStatus.BAD_REQUEST
        return {"ok": False, "message": str(loaded.get("message") or "data_ref rows를 찾지 못했습니다."), "ref": ref, "loaded": loaded, "status": status}
    return {"ok": True, "message": "", "ref": ref, "loaded": loaded}


def resolved_status(resolved: dict[str, Any]) -> HTTPStatus:
    status = resolved.get("status")
    return status if isinstance(status, HTTPStatus) else HTTPStatus.BAD_REQUEST


def data_ref_from_query(query: str) -> dict[str, Any] | None:
    params = parse_qs(query, keep_blank_values=False)
    token = first_param(params, "download_ref")
    if token:
        return decode_data_ref(token)
    ref_id = first_param(params, "ref_id") or first_param(params, "data_ref")
    if not ref_id:
        return None
    ref = {
        "store": "mongodb",
        "ref_id": ref_id,
        "database": first_param(params, "database"),
        "collection_name": first_param(params, "collection_name") or first_param(params, "collection"),
        "path": first_param(params, "path") or first_param(params, "row_path"),
        "role": first_param(params, "role"),
        "source_alias": first_param(params, "source_alias"),
        "label": first_param(params, "label"),
    }
    return {key: value for key, value in ref.items() if value not in (None, "")}


def first_param(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    return str(values[0] or "").strip() if values else ""


def decode_data_ref(token: str) -> dict[str, Any]:
    padded = token + "=" * (-len(token) % 4)
    payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("download_ref token is not an object.")
    return parsed


def encode_data_ref(ref: dict[str, Any]) -> str:
    payload = json.dumps(ref, ensure_ascii=False, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def render_data_page(
    ref: dict[str, Any],
    loaded: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    csv_url: str,
    json_url: str,
    preview_limit: int,
) -> str:
    row_count = int_or_zero(loaded.get("row_count")) or len(rows)
    title = ref_label(ref)
    table = render_table(rows[:preview_limit], columns)
    summary = [
        ("ref_id", ref.get("ref_id")),
        ("role", ref.get("role")),
        ("path", ref.get("path")),
        ("database", loaded.get("database") or ref.get("database")),
        ("collection", loaded.get("collection_name") or ref.get("collection_name")),
        ("expires_at", loaded.get("expires_at")),
        ("rows", row_count),
        ("columns", len(columns)),
    ]
    summary_html = "\n".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in summary if value not in (None, "", []))
    return page_shell(
        title,
        f"""
        <section class="toolbar">
          <a class="button primary" href="{escape(csv_url)}">CSV 다운로드</a>
          <a class="button" href="{escape(json_url)}">data_ref JSON 다운로드</a>
        </section>
        <dl class="summary">{summary_html}</dl>
        <p class="note">아래 표는 최대 {preview_limit:,}행 미리보기입니다. CSV 다운로드는 전체 rows를 내려받습니다.</p>
        {table}
        """,
    )


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return '<p class="empty">표시할 rows가 없습니다.</p>'
    head = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(row.get(column, ''))}</td>" for column in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'


def error_page(title: str, message: str) -> str:
    return page_shell(title, f'<p class="error">{escape(message)}</p><p class="note">링크의 download_ref 또는 서버 .env 설정을 확인하세요.</p>')


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Malgun Gothic", sans-serif; color: #17202a; background: #f6f7f9; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    .toolbar {{ display: flex; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }}
    .button {{ display: inline-block; padding: 9px 13px; border-radius: 6px; border: 1px solid #b9c0ca; color: #17202a; text-decoration: none; background: white; }}
    .button.primary {{ background: #1f6feb; color: white; border-color: #1f6feb; }}
    .summary {{ display: grid; grid-template-columns: 150px 1fr; gap: 6px 12px; background: white; padding: 14px; border: 1px solid #d8dee8; border-radius: 8px; }}
    .summary dt {{ font-weight: 700; color: #56606f; }}
    .summary dd {{ margin: 0; overflow-wrap: anywhere; }}
    .note {{ color: #5d6878; font-size: 13px; }}
    .error {{ background: #fff1f0; border: 1px solid #ffccc7; color: #a8071a; padding: 14px; border-radius: 8px; }}
    .table-wrap {{ overflow: auto; border: 1px solid #d8dee8; border-radius: 8px; background: white; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #e7ebf0; text-align: left; white-space: nowrap; }}
    th {{ position: sticky; top: 0; background: #eef2f7; z-index: 1; }}
    .empty {{ background: white; border: 1px solid #d8dee8; border-radius: 8px; padding: 14px; }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""


def rows_to_csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def ref_label(ref: dict[str, Any]) -> str:
    label = str(ref.get("label") or "").strip()
    if label:
        return label
    role = str(ref.get("role") or "").strip()
    alias = str(ref.get("source_alias") or ref.get("dataset_key") or "").strip()
    if role == "analysis_result":
        return "분석 결과 데이터"
    if role == "source_rows" and alias:
        return f"사용 원본 데이터: {alias}"
    return "MongoDB 저장 데이터"


def download_filename(ref: dict[str, Any], suffix: str) -> str:
    seed = str(ref.get("label") or ref.get("source_alias") or ref.get("role") or ref.get("ref_id") or "data_ref")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed).strip("._") or "data_ref"
    return f"{cleaned}.{suffix}"


def int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def load_dotenv(env_file: str | Path) -> None:
    path = Path(env_file)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
