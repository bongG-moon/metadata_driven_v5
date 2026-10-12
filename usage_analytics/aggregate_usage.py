"""Phoenix에서 MongoDB로 보관한 사용 이력을 읽기 전용으로 집계합니다.

사용 순서:
1. 패키지 설치: python -m pip install "pymongo>=4.6,<5"
2. 아래 '직접 입력하는 설정'에서 기간, 활성 기준, MongoDB 연결 정보를 설정합니다.
3. 실행: python aggregate_usage.py
4. 실행 완료 시 표시되는 HTML 파일을 브라우저로 열어 결과를 확인합니다.
   동일한 이름의 JSON에는 원본 이력과 집계 결과가 모두 저장됩니다.

MongoDB 데이터를 추가/수정/삭제하지 않으며 Phoenix를 직접 호출하지 않습니다.
결과에는 사번과 질문이 포함되므로 결과 파일을 공유할 때 주의하세요.
"""
from __future__ import annotations

import argparse
import json
import os
import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Any

# ===== 직접 입력하는 설정: 보통 이 영역만 수정하면 됩니다 =====

# [1] 전체 사용자/채팅 집계 기간: 따옴표 안에 YYYY-MM-DD 형식으로 입력합니다.
# 시작일과 종료일을 모두 포함하며, MongoDB에 저장된 한국 날짜(KST) 기준입니다.
# 예: 9월 전체는 시작 "2026-09-01", 종료 "2026-09-30"입니다.
START_DATE = "2026-09-01"
END_DATE = "2026-09-28"

# [2] 활성 사용자 기준: 따옴표 없이 1 이상의 정수를 입력합니다.
# 아래 예시는 '서로 다른 3일 이상 사용'하면서 '총 10건 이상 채팅'한 사람입니다.
# 같은 날 10건 사용해도 사용 일수는 1일입니다. 두 조건을 모두 만족해야 합니다.
MIN_USAGE_DAYS = 3
MIN_CHAT_COUNT = 10

# [3] 활성 사용자만 별도 기간으로 보고 싶을 때 설정합니다.
# 전체 집계와 같은 기간이면 두 값 모두 None으로 유지합니다(None은 따옴표 없음).
# 예: 전체는 9월, 활성은 최근 21일이면 아래를 각각 "2026-09-08", "2026-09-28"로 변경.
# 별도 기간을 지정할 때는 시작일과 종료일을 반드시 함께 입력하세요.
ACTIVE_START_DATE = None
ACTIVE_END_DATE = None

# [4] 프로젝트 선택: 전체 프로젝트는 None, 특정 프로젝트는 문자열 목록으로 입력합니다.
# 예: PROJECTS = ["프로젝트A", "프로젝트B"]
# 이름은 MongoDB source_project에 저장된 값과 정확히 같아야 합니다. []는 아무것도 조회하지 않습니다.
PROJECTS = None

# [5] MongoDB 접속 URI: 기본은 환경변수 MONGODB_URI에서 읽습니다.
# 코드에 직접 넣으려면 아래 줄 전체를 다음 형태로 바꾸세요(아래 주소는 예시입니다).
# MONGODB_URI = "mongodb+srv://YOUR_USER:YOUR_PASSWORD@YOUR_CLUSTER.mongodb.net/"
# 실제 URI가 들어간 코드는 Git/메일로 공유하지 마세요. 읽기 전용 계정을 권장합니다.
# 사용자명/비밀번호 안의 @, :, / 등 예약 문자는 URL 인코딩해야 합니다.
# .env 파일은 자동으로 읽지 않습니다. .env 사용 시 python-dotenv[cli] 설치 후:
# python -m dotenv -f .env run -- python aggregate_usage.py
MONGODB_URI = os.getenv("MONGODB_URI", "")

# [6] 데이터베이스: 기본값 datagov. 직접 고정하려면 MONGODB_DATABASE = "datagov"
# 환경변수가 설정되어 있으면 아래 기본값보다 환경변수 값이 우선합니다.
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "datagov")

# [7] 사용 이력 컬렉션: 기본값 portal_usage_history. 스케줄/메타데이터 컬렉션이 아닙니다.
# 직접 고정하려면 COLLECTION = "portal_usage_history"로 변경하세요.
COLLECTION = os.getenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")

# [8] 결과 파일 저장 폴더: 기본은 이 Python 파일 옆 reports 폴더입니다.
# 예: OUTPUT_DIRECTORY = Path("C:/Users/사용자명/Desktop/사용현황")
# 실행마다 날짜·시간이 붙은 HTML과 JSON을 새로 만듭니다. 두 파일 모두 사용자/질문을 포함합니다.
OUTPUT_DIRECTORY = Path(__file__).resolve().parent / "reports"

# ===== 아래는 집계 처리 코드: 일반 사용 시 수정할 필요가 없습니다 =====
# 출력: raw_records는 원본 질문 이력, usage/active_period의 daily는 일별 집계,
# users는 전체 사용자별 집계, active_users는 활성 기준을 충족한 사용자 명단입니다.
# 일 평균은 사용하지 않은 날도 포함한 기간 전체 일수로 나눕니다.
# raw_records는 최신 적재 기준의 보관 이력이며 Phoenix의 전체 원본 span은 아닙니다.
# 명령줄 --start/--end/--min-days/--min-chats를 주면 해당 값이 위 설정보다 우선합니다.


def _range(start: date, end: date) -> list[str]:
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def read_records(collection, start: date, end: date, projects: list[str] | None = None) -> list[dict]:
    """Only find(): no index creation, Phoenix refresh, or database writes."""
    _range(start, end)
    query = {"usage_date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if projects is not None:
        query["source_project"] = {"$in": projects}
    projection = {"_id": 0, **{key: 1 for key in (
        "record_type", "source_project", "usage_date", "query_time",
        "user_id", "user_name", "refresh_started_at", "platform", "question", "trace_id", "archive_mode",
    )}}
    documents = list(collection.find(query, projection))
    generations = {}
    merge_scopes = set()
    for row in documents:
        if row.get("record_type") == "usage_history_scope_marker":
            scope = (row.get("source_project"), row.get("usage_date"))
            if row.get("archive_mode") == "merge":
                merge_scopes.add(scope)
            generation = str(row.get("refresh_started_at") or "")
            generations[scope] = max(generations.get(scope, ""), generation)
    records = []
    for row in documents:
        if row.get("record_type") not in (None, "usage_history_record"):
            continue
        if not row.get("query_time") or not row.get("source_project"):
            continue
        generation = generations.get((row.get("source_project"), row.get("usage_date")))
        if (row.get("source_project"), row.get("usage_date")) not in merge_scopes and generation and str(row.get("refresh_started_at") or "") != generation:
            continue
        records.append(row)
    return records


def build_result(records: Iterable[Mapping[str, Any]], start: date, end: date,
                 *, active_start: date | None = None, active_end: date | None = None,
                 min_days: int = 3, min_chats: int = 10, projects=None) -> dict:
    if (active_start is None) != (active_end is None):
        raise ValueError("활성 사용자 시작일/종료일을 함께 입력하세요.")
    active_start, active_end = active_start or start, active_end or end
    usage_dates, active_dates = set(_range(start, end)), set(_range(active_start, active_end))
    records = [row for row in records if str(row.get("usage_date") or "") in usage_dates | active_dates]
    records.sort(key=lambda row: (str(row.get("query_time") or ""), str(row.get("source_project") or "")))
    raw = [{
        **{key: row.get(key, "") for key in ("source_project", "usage_date", "query_time", "platform", "user_id", "user_name", "question", "trace_id")},
        "in_usage_period": row.get("usage_date") in usage_dates,
        "in_active_period": row.get("usage_date") in active_dates,
    } for row in records]
    return {
        "projects": projects, "timezone": "Asia/Seoul",
        "raw_record_count": len(raw), "raw_records": raw,
        "usage": aggregate(records, start, end, min_days=min_days, min_chats=min_chats),
        "active_period": aggregate(records, active_start, active_end, min_days=min_days, min_chats=min_chats),
        "note": "원본은 Phoenix 전체 span이 아닌 MongoDB 보관 질문 이력입니다. 미수집 날짜는 0건, 사번 누락은 기존 Portal처럼 '미확인' 1명으로 집계합니다.",
    }


def aggregate(records: Iterable[Mapping[str, Any]], start: date, end: date,
              *, min_days: int = 3, min_chats: int = 10) -> dict:
    """Same Portal rule: distinct usage days >= N AND chat count >= M."""
    if min_days < 1 or min_chats < 1:
        raise ValueError("활성 사용자 기준 일수와 건수는 1 이상이어야 합니다.")
    days = _range(start, end)
    daily = {day: {"users": set(), "chats": 0} for day in days}
    users = defaultdict(lambda: {"name": "", "dates": set(), "chats": 0})
    unknown_chats = 0
    for row in records:
        day = str(row.get("usage_date") or "")
        if day not in daily:
            continue
        employee = str(row.get("user_id") or "").strip() or "미확인"
        unknown_chats += employee == "미확인"
        daily[day]["users"].add(employee)
        daily[day]["chats"] += 1
        user = users[employee]
        user["dates"].add(day)
        user["chats"] += 1
        if not user["name"]:
            user["name"] = str(row.get("user_name") or "")
    user_rows = [{
        "employee_id": employee, "user_name": values["name"],
        "usage_dates": sorted(values["dates"]), "distinct_days": len(values["dates"]),
        "chat_count": values["chats"],
        "daily_average": round(values["chats"] / len(days), 4),
        "usage_day_average": round(values["chats"] / len(values["dates"]), 4),
        "is_active": len(values["dates"]) >= min_days and values["chats"] >= min_chats,
    } for employee, values in users.items()]
    user_rows.sort(key=lambda row: (-row["chat_count"], -row["distinct_days"], row["employee_id"]))
    chats = sum(row["chats"] for row in daily.values())
    active = [row for row in user_rows if row["is_active"]]
    return {
        "start_date": start.isoformat(), "end_date": end.isoformat(), "day_count": len(days),
        "total_users": len(users), "total_chats": chats,
        "daily_average_users": round(sum(len(row["users"]) for row in daily.values()) / len(days), 4),
        "daily_average_chats": round(chats / len(days), 4),
        "active_user_rule": {"min_distinct_days": min_days, "min_chat_count": min_chats, "operator": "AND"},
        "active_user_count": len(active), "active_users": active, "users": user_rows,
        "daily": [{"date": day, "user_count": len(row["users"]), "chat_count": row["chats"]} for day, row in daily.items()],
        "unknown_user_chat_count": unknown_chats,
    }


def query_usage(uri: str, database: str, *, start: date, end: date,
                collection: str = "portal_usage_history", projects: list[str] | None = None,
                active_start: date | None = None, active_end: date | None = None,
                min_days: int = 3, min_chats: int = 10) -> dict:
    """Python/notebook entry point. Returns summary, all users and active users."""
    if not uri.strip() or not database.strip() or not collection.strip():
        raise ValueError("MongoDB URI, database, collection을 입력하세요.")
    if (active_start is None) != (active_end is None):
        raise ValueError("활성 사용자 시작일/종료일을 함께 입력하세요.")
    active_start, active_end = active_start or start, active_end or end
    _range(start, end)
    _range(active_start, active_end)
    if min_days < 1 or min_chats < 1:
        raise ValueError("활성 사용자 기준은 1 이상이어야 합니다.")
    from pymongo import MongoClient
    with MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=30000) as client:
        records = read_records(client[database][collection], min(start, active_start), max(end, active_end), projects)
    return build_result(records, start, end, active_start=active_start, active_end=active_end,
                        min_days=min_days, min_chats=min_chats, projects=projects)


def render_report(result: Mapping[str, Any]) -> str:
    """외부 CDN 없이 열 수 있는 HTML. 모든 원본 문자열은 HTML 이스케이프합니다."""
    def escape(value):
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if isinstance(value, bool):
            value = "예" if value else "아니오"
        return html.escape(str(value if value is not None else ""), quote=True)

    usage, active = result["usage"], result["active_period"]
    period = lambda item: f'{escape(item["start_date"])} ~ {escape(item["end_date"])}'
    cards = "".join(f'<div class="card"><span>{label}</span><strong>{escape(value)}<small>{unit}</small></strong></div>' for label, value, unit in (
        ("전체 사용자", usage["total_users"], "명"), ("전체 채팅", usage["total_chats"], "건"),
        ("일 평균 사용자", usage["daily_average_users"], "명"), ("일 평균 채팅", usage["daily_average_chats"], "건"),
    ))
    def table(title, description, rows, columns):
        headers = "".join(f'<th scope="col">{escape(label)}</th>' for key, label in columns)
        body = "".join('<tr>' + "".join(f'<td>{escape(row.get(key, ""))}</td>' for key, label in columns) + '</tr>' for row in rows)
        return f'''<section><h2>{escape(title)}</h2><p>{escape(description)}</p>
<div class="tools"><label>검색 <input type="search" placeholder="사번·이름·질문 등 검색" aria-label="{escape(title)} 검색"></label><span class="count">전체 {len(rows):,}건</span></div>
<div class="scroll"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div>
<p class="empty" hidden>표시할 데이터가 없습니다.</p><div class="pager"><button class="prev" type="button">이전</button><span class="page" aria-live="polite"></span><button class="next" type="button">다음</button></div></section>'''
    user_columns = [("employee_id", "사번"), ("user_name", "이름"), ("distinct_days", "사용 일수"), ("chat_count", "채팅 건수"), ("daily_average", "일 평균(전체 기간)"), ("usage_day_average", "일 평균(사용일)"), ("is_active", "활성 여부"), ("usage_dates", "사용 일자")]
    daily_columns = [("date", "일자"), ("user_count", "사용자 수"), ("chat_count", "채팅 건수")]
    sections = table("일별 사용 현황", f'{usage["start_date"]} ~ {usage["end_date"]} · 0건 날짜 포함', usage["daily"], daily_columns)
    sections += table("전체 사용자별 집계", "전체 집계 기간 기준 · 활성 여부도 이 기간 기준입니다.", usage["users"], user_columns)
    sections += table("활성 사용자 명단", f'{active["start_date"]} ~ {active["end_date"]} · 아래 활성 기준을 충족한 사용자', active["active_users"], user_columns)
    if (usage["start_date"], usage["end_date"]) != (active["start_date"], active["end_date"]):
        sections += table("활성 집계 기간 · 전체 사용자", "별도 활성 집계 기간 내 사용자별 집계", active["users"], user_columns)
        sections += table("활성 집계 기간 · 일별 현황", "별도 활성 집계 기간 내 일별 집계", active["daily"], daily_columns)
    sections += table("원본 보관 이력", "두 집계 기간에 해당하는 최신 MongoDB 보관 이력입니다. 검색·페이지 이동으로 전체 행을 확인할 수 있습니다.", result["raw_records"], [
        ("source_project", "프로젝트"), ("usage_date", "일자"), ("query_time", "조회 시각"), ("platform", "플랫폼"),
        ("user_id", "사번"), ("user_name", "이름"), ("question", "질문"), ("trace_id", "Trace ID"),
        ("in_usage_period", "전체 기간 포함"), ("in_active_period", "활성 기간 포함"),
    ])
    rule = active["active_user_rule"]
    project_label = "전체 프로젝트" if result.get("projects") is None else ", ".join(result["projects"]) or "선택 없음"
    return '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PTMORE 사용 현황 보고서</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f5fa;color:#24314a;font:14px/1.65 "Malgun Gothic",Arial,sans-serif}main{max-width:1440px;margin:auto;padding:40px 24px}header{margin-bottom:24px}h1{font-size:30px;letter-spacing:-1px;margin:4px 0}h2{font-size:19px;margin:0 0 8px}p{color:#64748b;margin:6px 0 18px}.eyebrow{color:#6475ac;font-size:12px;font-weight:bold;letter-spacing:2px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.card,section,.active{background:white;border:1px solid #e2e7f0;border-radius:16px;padding:24px}.card span{color:#64748b}.card strong{display:block;font-size:30px;margin-top:12px;color:#354679}.card small{font-size:15px;margin-left:6px}.active{margin:18px 0;background:#edf1fb}.active strong{color:#405999}section{margin-top:22px;min-width:0}.tools,.pager{display:flex;gap:12px;align-items:center;justify-content:space-between;margin:16px 0}input{border:1px solid #ced7e8;border-radius:8px;padding:9px 12px;font:inherit;max-width:100%}.count,.page{color:#64748b}.scroll{overflow:auto;max-height:560px;border:1px solid #e2e7f0;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th{position:sticky;top:0;background:#edf1f8;color:#51627e;white-space:nowrap}td,th{padding:12px 14px;border-bottom:1px solid #e7ebf3}td{min-width:100px;max-width:400px;overflow-wrap:anywhere;white-space:pre-wrap}tr:nth-child(even){background:#fafbfe}button{border:1px solid #cfd8ea;background:#f7f9fe;color:#405999;border-radius:8px;padding:7px 16px;font:inherit;cursor:pointer}button:disabled{opacity:.4;cursor:default}.pager{justify-content:flex-end}.empty{padding:16px}footer{padding:24px 0;color:#718096;font-size:12px}[hidden]{display:none!important}@media(max-width:700px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:20px 12px}.card,section{padding:16px}.tools{flex-wrap:wrap}h1{font-size:24px}}@media print{.tools,.pager{display:none}.scroll{max-height:none;overflow:visible}tr[hidden]{display:table-row!important}section{break-inside:auto}}
</style></head><body><main>''' + f'''<header><div class="eyebrow">PTMORE · USAGE ANALYTICS</div><h1>사용 현황 보고서</h1><p>{period(usage)} · 한국 날짜(KST) · {usage["day_count"]}일 · {escape(project_label)}</p></header>
<div class="cards">{cards}</div><div class="active"><strong>활성 사용자 {active["active_user_count"]}명</strong><br>집계 기간 {period(active)} · 사용 {rule["min_distinct_days"]}일 이상 <b>그리고</b> 채팅 {rule["min_chat_count"]}건 이상</div>
<p>{escape(result.get("note", ""))} 전체 기간 사번 미확인 채팅: {usage["unknown_user_chat_count"]}건.</p>{sections}<footer>원본 질문과 사번이 포함된 보고서입니다. 권한이 있는 사람에게만 공유하세요. 외부 연결 없이 열 수 있습니다.</footer></main>''' + '''<script>
document.querySelectorAll('section').forEach(section=>{
const rows=Array.from(section.querySelectorAll('tbody tr'));const input=section.querySelector('input');let page=0;const size=25;
function draw(){const query=input.value.trim().toLocaleLowerCase();const matches=rows.filter(row=>row.textContent.toLocaleLowerCase().includes(query));const pages=Math.max(1,Math.ceil(matches.length/size));page=Math.min(page,pages-1);rows.forEach(row=>row.hidden=true);matches.slice(page*size,(page+1)*size).forEach(row=>row.hidden=false);section.querySelector('.count').textContent=`검색 ${matches.length}건 / 전체 ${rows.length}건`;section.querySelector('.page').textContent=`${page+1} / ${pages} 페이지 · 25건씩`;section.querySelector('.prev').disabled=page===0;section.querySelector('.next').disabled=page>=pages-1;section.querySelector('.empty').hidden=matches.length>0;}
input.addEventListener('input',()=>{page=0;draw()});section.querySelector('.prev').addEventListener('click',()=>{page--;draw()});section.querySelector('.next').addEventListener('click',()=>{page++;draw()});draw();});
</script></body></html>'''


def save_report(result: Mapping[str, Any], directory: Path | str) -> tuple[Path, Path]:
    """HTML과 원본/집계 JSON을 함께 저장하고 절대 경로를 반환합니다."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stem = "usage_report_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    html_path, json_path = directory / (stem + ".html"), directory / (stem + ".json")
    report = render_report(result)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    with json_path.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    with html_path.open("x", encoding="utf-8") as stream:
        stream.write(report)
    return html_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=START_DATE, type=date.fromisoformat)
    parser.add_argument("--end", default=END_DATE, type=date.fromisoformat)
    parser.add_argument("--active-start", default=ACTIVE_START_DATE, type=date.fromisoformat)
    parser.add_argument("--active-end", default=ACTIVE_END_DATE, type=date.fromisoformat)
    parser.add_argument("--min-days", type=int, default=MIN_USAGE_DAYS)
    parser.add_argument("--min-chats", type=int, default=MIN_CHAT_COUNT)
    parser.add_argument("--project", action="append", help="여러 프로젝트는 옵션을 반복 입력. 생략하면 전체")
    parser.add_argument("--database", default=MONGODB_DATABASE)
    parser.add_argument("--collection", default=COLLECTION)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()
    try:
        result = query_usage(MONGODB_URI, args.database,
            start=args.start, end=args.end, collection=args.collection, projects=args.project if args.project is not None else PROJECTS,
            active_start=args.active_start, active_end=args.active_end,
            min_days=args.min_days, min_chats=args.min_chats)
    except ValueError as exc:
        parser.error(str(exc))
    except Exception:
        # Connection exceptions may contain credentials/hosts. Do not echo them.
        parser.exit(1, "MongoDB 집계 실패: pymongo 설치와 접속 설정/읽기 권한을 확인하세요.\n")
    try:
        html_path, json_path = save_report(result, args.output_dir)
    except OSError:
        parser.exit(1, "보고서 저장 실패: 출력 폴더 쓰기 권한과 디스크 공간을 확인하세요.\n")
    print(f"HTML 보고서: {html_path}\n원본 및 집계 JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
