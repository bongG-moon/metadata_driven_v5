from datetime import date
import pytest
from aggregate_usage import aggregate, read_records


def test_counts_and_distinct_day_and_zero_day():
    rows = [{"usage_date": day, "user_id": user} for day, user in [
        ("2026-09-01", "1"), ("2026-09-01", "1"), ("2026-09-02", "1"),
        ("2026-09-02", "2"), ("2026-08-31", "3")]]
    result = aggregate(rows, date(2026, 9, 1), date(2026, 9, 3), min_days=2, min_chats=3)
    assert result["total_users"] == 2
    assert result["total_chats"] == 4
    assert result["daily_average_users"] == 1
    assert result["daily_average_chats"] == 1.3333
    assert result["active_user_count"] == 1
    assert result["active_users"][0]["usage_dates"] == ["2026-09-01", "2026-09-02"]
    assert result["daily"][-1]["chat_count"] == 0


def test_read_generation_fence_and_project_filter():
    class Collection:
        def find(self, query, projection):
            assert query["source_project"] == {"$in": ["A"]}
            assert projection["question"] == 1 and projection["platform"] == 1
            common = {"source_project": "A", "usage_date": "2026-09-01", "query_time": "2026-09-01T00:00:00+09:00"}
            return [{**common, "record_type": "usage_history_scope_marker", "refresh_started_at": "new"},
                    {**common, "refresh_started_at": "old"}, {**common, "refresh_started_at": "new"}]
    assert len(read_records(Collection(), date(2026,9,1), date(2026,9,2), ["A"])) == 1


def test_empty_and_invalid_and_unknown():
    day = date(2026, 9, 1)
    assert aggregate([], day, day)["total_chats"] == 0
    with pytest.raises(ValueError):
        aggregate([], day, date(2026, 8, 1))
    with pytest.raises(ValueError):
        aggregate([], day, day, min_days=0)
    assert aggregate([{"usage_date": day.isoformat()}], day, day)["unknown_user_chat_count"] == 1


def test_raw_data_and_separate_periods():
    from aggregate_usage import build_result
    rows = [{"usage_date": f"2026-09-{day:02}", "user_id": "1", "question": "질문", "platform": "CUBE"} for day in (1, 2, 3)]
    result = build_result(rows, date(2026,9,1), date(2026,9,1), active_start=date(2026,9,3), active_end=date(2026,9,3), min_days=1, min_chats=1)
    assert result["raw_record_count"] == 2
    assert result["usage"]["total_chats"] == 1
    assert result["active_period"]["active_user_count"] == 1
    assert result["raw_records"][0]["question"] == "질문"
    assert result["raw_records"][0]["in_usage_period"] is True
    assert result["raw_records"][1]["in_usage_period"] is False


def test_main_without_arguments_uses_top_settings(monkeypatch, capsys, tmp_path):
    import aggregate_usage as module
    import sys
    monkeypatch.setattr(sys, "argv", ["aggregate_usage.py"])
    monkeypatch.setattr(module, "START_DATE", "2026-09-02")
    monkeypatch.setattr(module, "END_DATE", "2026-09-04")
    monkeypatch.setattr(module, "MIN_USAGE_DAYS", 2)
    monkeypatch.setattr(module, "MIN_CHAT_COUNT", 5)
    monkeypatch.setattr(module, "OUTPUT_DIRECTORY", tmp_path)
    def query(uri, database, **kwargs):
        assert kwargs["start"] == date(2026,9,2)
        assert kwargs["end"] == date(2026,9,4)
        assert (kwargs["min_days"], kwargs["min_chats"]) == (2, 5)
        return module.build_result([], kwargs["start"], kwargs["end"])
    monkeypatch.setattr(module, "query_usage", query)
    assert module.main() == 0
    assert "HTML" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.html"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_html_report_preserves_rows_and_escapes_untrusted_data(tmp_path):
    import json
    from aggregate_usage import build_result, save_report
    question = '<script>alert("test")</script> & 생산량'
    rows = [{"usage_date": "2026-09-01", "user_id": "0012345", "question": question} for _ in range(30)]
    result = build_result(rows, date(2026,9,1), date(2026,9,2), min_days=1, min_chats=1)
    report, raw = save_report(result, tmp_path)
    text = report.read_text(encoding="utf-8")
    assert '<script>alert' not in text and '&lt;script&gt;' in text
    assert text.count('생산량') == 30
    assert '0012345' in text and '원본 보관 이력' in text
    assert json.loads(raw.read_text(encoding="utf-8"))["raw_record_count"] == 30
    assert json.loads(raw.read_text(encoding="utf-8"))["raw_records"][0]["question"] == question
    other, _ = save_report(result, tmp_path)
    assert other != report
