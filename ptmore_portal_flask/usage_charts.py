"""KST calendar month, ISO week and day summaries from archived requests."""
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def month_start(today, offset=0):
    month = today.year * 12 + today.month - 1 + offset
    return date(month // 12, month % 12 + 1, 1)


def chart_start(today):
    return min(month_start(today, -2), today - timedelta(days=today.weekday() + 21))


def build_charts(records, today):
    normalized = []
    seen = set()
    for record in records:
        try:
            value = str(record.get("query_time") or record.get("occurred_at") or "")
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            day = (stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp.astimezone(KST)).date()
        except ValueError:
            continue
        key = (record.get("project") or record.get("source_project"), record.get("trace_id") or record.get("_id") or (value, record.get("user_id"), record.get("question")))
        if key in seen:
            continue
        seen.add(key)
        normalized.append((day, str(record.get("user_id") or "").strip()))
    monday = today - timedelta(days=today.weekday())
    ranges = {
        "monthly": [(month_start(today, i), min(today, month_start(today, i + 1) - timedelta(days=1))) for i in (-2, -1, 0)],
        "weekly": [(monday + timedelta(weeks=i), min(today, monday + timedelta(weeks=i + 1, days=-1))) for i in (-3, -2, -1, 0)],
        "daily": [(today - timedelta(days=i), today - timedelta(days=i)) for i in range(13, -1, -1)],
    }
    result = {}
    for kind, periods in ranges.items():
        rows = []
        for start, end in periods:
            selected = [user for day, user in normalized if start <= day <= end]
            iso = start.isocalendar()
            label = start.strftime("%Y-%m") if kind == "monthly" else f"{iso.year}-W{iso.week:02}" if kind == "weekly" else start.strftime("%m/%d")
            rows.append({"label": label, "start": start.isoformat(), "end": end.isoformat(),
                         "unique_users": len({u for u in selected if u}), "chat_count": len(selected)})
        result[kind] = rows
    return result
