from __future__ import annotations

from tools.benchmark_langflow_call_paths import BenchmarkSample, summarize_samples


def test_benchmark_summary_reports_five_run_latency_and_failures() -> None:
    samples = [
        BenchmarkSample("router_api", index, f"s-{index}", elapsed, True, 200, 10, "")
        for index, elapsed in enumerate((100, 200, 300, 400, 500), start=1)
    ]
    samples.append(BenchmarkSample("native_run_flow", 1, "rf-1", 300000, False, None, 0, "Timeout"))

    summary = summarize_samples(samples)

    assert summary["router_api"] == {
        "runs": 5,
        "successes": 5,
        "failures": 0,
        "min_ms": 100,
        "p50_ms": 300,
        "p95_ms": 500,
        "max_ms": 500,
    }
    assert summary["native_run_flow"]["failures"] == 1
    assert summary["native_run_flow"]["p50_ms"] is None
