"""최근 21일 더미 사용 이력 생성. --apply를 지정해야 MongoDB에 저장합니다."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from runtime_settings import get_setting

KST = timezone(timedelta(hours=9))
DEMO_PROJECT = "PTMORE_DEMO_USAGE"


def build_records(today=None):
    today = today or datetime.now(KST).date()
    questions = ["DA 공정 생산 현황 알려줘", "오늘 WIP 현황 보여줘", "설비 가동률 조회해줘", "등록된 데이터셋 알려줘"]
    rows = []
    for offset in range(21):
        day = today - timedelta(days=offset)
        for user in range(12):
            # 자주 쓰는 사용자와 가끔 쓰는 사용자를 섞어 활성 사용자 비교가 가능합니다.
            if user >= 8 and offset != user - 8:
                continue
            if user < 8 and (day.toordinal() + user) % 4 == 0:
                continue
            for chat in range(1 + (day.toordinal() + user) % 4):
                trace = f"demo-{day.isoformat()}-{user:02}-{chat}"
                rows.append({
                    "_id": f"ptmore-demo:{trace}", "record_type": "usage_history_record",
                    "source_project": DEMO_PROJECT, "trace_id": trace,
                    "usage_date": day.isoformat(),
                    "query_time": f"{day.isoformat()}T{8 + user % 10:02}:{chat * 12:02}:00+09:00",
                    "platform": ["GaiA_Internal", "CUBE", "CUBE_SCHEDULING"][user % 3],
                    "user_id": str(9900001 + user), "user_name": f"테스트사용자{user + 1:02}",
                    "question": "[더미 이력] " + questions[(user + chat) % len(questions)],
                    "is_demo": True, "demo_batch": "ptmore_usage_v1",
                    "archived_at": datetime.now(KST).isoformat(),
                })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = build_records()
    summary = {"records": len(rows), "users": len({r["user_id"] for r in rows}),
               "start": min(r["usage_date"] for r in rows), "end": max(r["usage_date"] for r in rows)}
    if not args.apply:
        print(json.dumps({**summary, "saved": False}, ensure_ascii=False))
        return
    from pymongo import MongoClient, UpdateOne
    uri = get_setting("MONGODB_URI")
    database = get_setting("MONGODB_DATABASE")
    collection = get_setting("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")
    if not uri or database != "datagov" or collection != "portal_usage_history":
        raise SystemExit("datagov.portal_usage_history 연결 설정을 확인해 주세요. 비밀값은 출력하지 않습니다.")
    with MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=20000) as client:
        target = client[database][collection]
        client.admin.command("ping")
        # 같은 날짜의 더미 이력은 재실행해도 중복 생성하거나 덮어쓰지 않습니다.
        result = target.bulk_write([UpdateOne({"_id": row["_id"]}, {"$setOnInsert": row}, upsert=True) for row in rows])
        stored = list(target.find({"_id": {"$in": [r["_id"] for r in rows]}}, {"_id": 1, "is_demo": 1}))
        assert len(stored) == len(rows) and all(row.get("is_demo") for row in stored)
        print(json.dumps({**summary, "saved": True, "inserted": result.upserted_count,
                          "verified": len(stored), "collection": f"{database}.{collection}"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"더미 이력 저장 실패: {type(exc).__name__}. 연결 설정과 접근 권한을 확인해 주세요.") from None
