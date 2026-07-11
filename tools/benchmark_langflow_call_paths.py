from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_QUESTION = "오늘 DA공정 생산량 알려줘"
DEFAULT_REPEATS = 5
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_READ_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class BenchmarkSample:
    path: str
    run: int
    session_id: str
    elapsed_ms: int
    ok: bool
    status_code: int | None
    response_chars: int
    error: str


def run_sample(
    path_name: str,
    url: str,
    question: str,
    run_number: int,
    *,
    api_key: str = "",
    read_timeout_seconds: int = DEFAULT_READ_TIMEOUT_SECONDS,
    shared_session_id: str = "",
) -> BenchmarkSample:
    session_id = shared_session_id or f"benchmark-{path_name}-{run_number}-{uuid.uuid4()}"
    payload = {
        "input_value": question,
        "input_type": "chat",
        "output_type": "chat",
        "session_id": session_id,
    }
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["x-api-key"] = str(api_key).strip()
    started = time.perf_counter()
    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, max(1, int(read_timeout_seconds))),
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response_text = response.text or ""
        response.raise_for_status()
        return BenchmarkSample(
            path=path_name,
            run=run_number,
            session_id=session_id,
            elapsed_ms=elapsed_ms,
            ok=True,
            status_code=response.status_code,
            response_chars=len(response_text),
            error="",
        )
    except Exception as exc:
        return BenchmarkSample(
            path=path_name,
            run=run_number,
            session_id=session_id,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
            response_chars=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def summarize_samples(samples: list[BenchmarkSample]) -> dict[str, Any]:
    by_path: dict[str, list[BenchmarkSample]] = {}
    for sample in samples:
        by_path.setdefault(sample.path, []).append(sample)
    summary: dict[str, Any] = {}
    for path_name, path_samples in by_path.items():
        successful_ms = sorted(sample.elapsed_ms for sample in path_samples if sample.ok)
        summary[path_name] = {
            "runs": len(path_samples),
            "successes": len(successful_ms),
            "failures": len(path_samples) - len(successful_ms),
            "min_ms": successful_ms[0] if successful_ms else None,
            "p50_ms": int(statistics.median(successful_ms)) if successful_ms else None,
            "p95_ms": _nearest_rank(successful_ms, 0.95),
            "max_ms": successful_ms[-1] if successful_ms else None,
        }
    return summary


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    rank = max(1, int(len(values) * percentile + 0.999999))
    return values[min(rank, len(values)) - 1]


def benchmark(
    paths: dict[str, str],
    question: str,
    *,
    repeats: int = DEFAULT_REPEATS,
    api_key: str = "",
    read_timeout_seconds: int = DEFAULT_READ_TIMEOUT_SECONDS,
    shared_session: bool = False,
) -> list[BenchmarkSample]:
    samples: list[BenchmarkSample] = []
    shared_ids = {name: f"benchmark-shared-{name}-{uuid.uuid4()}" for name in paths} if shared_session else {}
    ordered_paths = list(paths.items())
    for run_number in range(1, max(1, int(repeats)) + 1):
        current_paths = ordered_paths if run_number % 2 else list(reversed(ordered_paths))
        for path_name, url in current_paths:
            samples.append(
                run_sample(
                    path_name,
                    url,
                    question,
                    run_number,
                    api_key=api_key,
                    read_timeout_seconds=read_timeout_seconds,
                    shared_session_id=shared_ids.get(path_name, ""),
                )
            )
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the same question against direct, Router API, and optional Native Run Flow wrapper paths."
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--direct-url", default="")
    parser.add_argument("--router-url", default="")
    parser.add_argument("--run-flow-url", default="")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_READ_TIMEOUT_SECONDS)
    parser.add_argument("--label", default="current")
    parser.add_argument("--shared-session", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    paths = {
        name: str(url).strip()
        for name, url in (
            ("data_analysis_direct", args.direct_url),
            ("router_api", args.router_url),
            ("native_run_flow", args.run_flow_url),
        )
        if str(url).strip()
    }
    if not paths:
        parser.error("Provide at least one of --direct-url, --router-url, or --run-flow-url.")

    samples = benchmark(
        paths,
        args.question,
        repeats=args.repeats,
        api_key=os.getenv("LANGFLOW_API_KEY", ""),
        read_timeout_seconds=args.timeout_seconds,
        shared_session=args.shared_session,
    )
    payload = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "question": args.question,
        "repeats": args.repeats,
        "timeout_seconds": args.timeout_seconds,
        "session_mode": "shared_per_path" if args.shared_session else "fresh_per_request",
        "summary": summarize_samples(samples),
        "samples": [asdict(sample) for sample in samples],
    }
    output = args.output or Path("benchmark_results") / f"langflow_paths_{args.label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), **payload["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
