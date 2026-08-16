#!/usr/bin/env python3
"""Benchmark two prompt-layout bundles with the real representative Flow path.

The benchmark keeps every Flow component and representative-case expectation
identical.  Only the Intent, pandas generation, pandas repair, and answer
template texts change between variants.  Gemini is called through its streaming
REST endpoint so the report can preserve both time-to-first-content and total
request latency, plus the provider-reported prompt-cache token count when it is
available.

No prompt, model response, API key, or MongoDB credential is written to the
report.  The report contains only questions, validation outcomes, hashes,
latencies, and token counters.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import statistics
import string
import sys
import time
from typing import Any, Callable, Iterator
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_data_analysis_v2_routes as routes  # noqa: E402
from tools import validate_representative_questions as base  # noqa: E402


DEFAULT_CASE_IDS = (1, 5, 10, 13, 22, 25, 30)
INTENT_TEMPLATE_NAME = "03_intent_prompt_template_ko.md"
TEMPLATE_FILES = {
    "intent": INTENT_TEMPLATE_NAME,
    "pandas_generation": "16_pandas_prompt_template_ko.md",
    "pandas_repair": "17b_pandas_repair_prompt_template_ko.md",
    "answer_generation": "19_answer_prompt_template_ko.md",
}
USAGE_INTEGER_FIELDS = (
    "promptTokenCount",
    "cachedContentTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "totalTokenCount",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def template_format_fields(value: str) -> list[str]:
    """Return the stable set of Flow variables referenced by a template."""

    return sorted({
        str(field_name)
        for _, field_name, _, _ in string.Formatter().parse(value)
        if field_name
    })


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage_metadata(value: Any) -> dict[str, Any]:
    """Keep non-secret Gemini token counters used by the A/B comparison."""

    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {
        key: _non_negative_int(source.get(key)) for key in USAGE_INTEGER_FIELDS
    }
    for key in ("promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails"):
        details = source.get(key)
        result[key] = deepcopy(details) if isinstance(details, list) else []
    prompt_tokens = result["promptTokenCount"]
    cached_tokens = result["cachedContentTokenCount"]
    result["cache_hit"] = cached_tokens > 0
    result["cache_read_ratio"] = round(cached_tokens / prompt_tokens, 6) if prompt_tokens else 0.0
    return result


def classify_prompt_stage(prompt: str) -> str:
    """Classify the existing Flow prompt without changing its contents."""

    head = str(prompt or "").lstrip()[:500]
    if "intent planner" in head:
        return "intent"
    if "pandas code repair agent" in head:
        return "pandas_repair"
    if "pandas code generator" in head:
        return "pandas_generation"
    if "분석 결과를 한국어로 답변" in head:
        return "answer_generation"
    return "unknown"


def _extract_stream_payload(payload: Any) -> tuple[str, dict[str, Any]]:
    """Extract one Gemini SSE event's text delta and optional usage metadata."""

    if not isinstance(payload, dict):
        return "", {}
    candidates = payload.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    content = candidate.get("content") if isinstance(candidate, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    text = "".join(
        str(part.get("text") or "")
        for part in parts if isinstance(part, dict)
    )
    usage = payload.get("usageMetadata")
    return text, usage if isinstance(usage, dict) else {}


def call_gemini_streaming(
    prompt: str,
    config: dict[str, Any],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[str, dict[str, Any]]:
    """Call Gemini once and return text plus TTFT/latency/cache measurements."""

    model = str(config["model"]).removeprefix("models/")
    encoded_model = urllib.parse.quote(model, safe="")
    encoded_key = urllib.parse.quote(str(config["api_key"]), safe="")
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:"
        f"streamGenerateContent?alt=sse&key={encoded_key}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": config["temperature"],
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = clock()
    first_content_at: float | None = None
    chunks: list[str] = []
    usage: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                serialized = line[5:].strip()
                if not serialized or serialized == "[DONE]":
                    continue
                try:
                    event = json.loads(serialized)
                except json.JSONDecodeError:
                    continue
                text_delta, event_usage = _extract_stream_payload(event)
                if text_delta:
                    if first_content_at is None:
                        first_content_at = clock()
                    chunks.append(text_delta)
                if event_usage:
                    usage = event_usage
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(
                error_payload.get("error", {}).get("message")
                if isinstance(error_payload, dict)
                else ""
            ).strip()
        except Exception:
            detail = ""
        suffix = f": {detail[:500]}" if detail else ""
        raise RuntimeError(f"Gemini request failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc

    finished = clock()
    text = "".join(chunks)
    if not text.strip():
        raise RuntimeError("Gemini streaming response did not contain text")
    total_ms = round((finished - started) * 1000, 3)
    ttft_ms = round(((first_content_at or finished) - started) * 1000, 3)
    return text, {
        "stage": classify_prompt_stage(prompt),
        "ttft_ms": ttft_ms,
        "total_latency_ms": total_ms,
        "prompt_chars": len(prompt),
        "prompt_sha256": _sha256_text(prompt),
        "usage": normalize_usage_metadata(usage),
    }


class CallRecorder:
    """Adapter matching the existing validator's ``call_llm`` signature."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, config: dict[str, Any]) -> str:
        text, measurement = call_gemini_streaming(prompt, config)
        self.calls.append(measurement)
        return text


@contextmanager
def injected_templates(
    templates: dict[str, str],
    recorder: Callable[[str, dict[str, Any]], str],
) -> Iterator[None]:
    """Inject four templates into the unmodified representative Flow validator."""

    original_call = base.call_llm
    original_read_text = Path.read_text
    by_filename = {
        TEMPLATE_FILES[stage]: templates[stage]
        for stage in TEMPLATE_FILES
    }

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        injected = by_filename.get(path.name)
        if injected is not None:
            return injected
        return original_read_text(path, *args, **kwargs)

    Path.read_text = read_text
    base.call_llm = recorder
    try:
        yield
    finally:
        Path.read_text = original_read_text
        base.call_llm = original_call


def _validation_error(case: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "id": int(case["id"]),
        "question": str(case["question"]),
        "status": "error",
        "errors": [f"{type(exc).__name__}: {exc}"],
    }


def run_variant(
    *,
    name: str,
    templates: dict[str, str],
    cases: list[dict[str, Any]],
    manifest: dict[int, dict[str, Any]],
    modules: dict[str, Any],
    v2_modules: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    """Run one prompt variant through the real live representative path."""

    results: list[dict[str, Any]] = []
    recorder = CallRecorder()
    for case in cases:
        call_offset = len(recorder.calls)
        started = time.perf_counter()
        try:
            with injected_templates(templates, recorder):
                result = routes.validate_live_case(
                    case,
                    manifest[int(case["id"])],
                    modules,
                    v2_modules,
                    metadata_context,
                    llm_config,
                    reference_date,
                )
        except Exception as exc:
            result = _validation_error(case, exc)
        result = deepcopy(result)
        result["wall_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["model_measurements"] = deepcopy(recorder.calls[call_offset:])
        results.append(result)
    return {
        "name": name,
        "template_contract": {
            stage: {
                "filename": TEMPLATE_FILES[stage],
                "sha256": _sha256_text(templates[stage]),
                "chars": len(templates[stage]),
                "format_fields": template_format_fields(templates[stage]),
                "call_count": sum(
                    measurement.get("stage") == stage
                    for measurement in recorder.calls
                ),
            }
            for stage in TEMPLATE_FILES
        },
        "results": results,
        "summary": summarize_variant(results),
    }


def _median(values: list[float]) -> float:
    return round(float(statistics.median(values)), 3) if values else 0.0


def summarize_variant(results: list[dict[str, Any]]) -> dict[str, Any]:
    measurements = [
        item
        for result in results
        for item in result.get("model_measurements", [])
        if isinstance(item, dict)
    ]
    intent = [item for item in measurements if item.get("stage") == "intent"]
    usage = [item.get("usage", {}) for item in intent]
    prompt_tokens = sum(_non_negative_int(item.get("promptTokenCount")) for item in usage)
    cached_tokens = sum(_non_negative_int(item.get("cachedContentTokenCount")) for item in usage)
    stage_summaries = {
        stage: summarize_stage(
            [item for item in measurements if item.get("stage") == stage]
        )
        for stage in TEMPLATE_FILES
    }
    return {
        "case_count": len(results),
        "passed": sum(item.get("status") == "ok" for item in results),
        "failed": sum(item.get("status") != "ok" for item in results),
        "model_call_count": len(measurements),
        "intent_call_count": len(intent),
        "intent_median_ttft_ms": _median([float(item.get("ttft_ms") or 0) for item in intent]),
        "intent_median_total_latency_ms": _median(
            [float(item.get("total_latency_ms") or 0) for item in intent]
        ),
        "intent_prompt_tokens": prompt_tokens,
        "intent_cached_content_tokens": cached_tokens,
        "intent_cache_hit_calls": sum(bool(item.get("cache_hit")) for item in usage),
        "intent_cache_read_ratio": round(cached_tokens / prompt_tokens, 6) if prompt_tokens else 0.0,
        "case_median_wall_latency_ms": _median(
            [float(item.get("wall_latency_ms") or 0) for item in results]
        ),
        "stage_summaries": stage_summaries,
    }


def summarize_stage(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one model stage while preserving a zero-call repair stage."""

    usage = [item.get("usage", {}) for item in measurements]
    prompt_tokens = sum(_non_negative_int(item.get("promptTokenCount")) for item in usage)
    cached_tokens = sum(_non_negative_int(item.get("cachedContentTokenCount")) for item in usage)
    return {
        "call_count": len(measurements),
        "median_ttft_ms": _median([float(item.get("ttft_ms") or 0) for item in measurements]),
        "median_total_latency_ms": _median(
            [float(item.get("total_latency_ms") or 0) for item in measurements]
        ),
        "prompt_tokens": prompt_tokens,
        "cached_content_tokens": cached_tokens,
        "candidate_tokens": sum(
            _non_negative_int(item.get("candidatesTokenCount")) for item in usage
        ),
        "thought_tokens": sum(
            _non_negative_int(item.get("thoughtsTokenCount")) for item in usage
        ),
        "total_tokens": sum(_non_negative_int(item.get("totalTokenCount")) for item in usage),
        "cache_hit_calls": sum(bool(item.get("cache_hit")) for item in usage),
        "cache_read_ratio": round(cached_tokens / prompt_tokens, 6) if prompt_tokens else 0.0,
    }


def compare_variants(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_by_id = {int(item["id"]): item for item in before.get("results", [])}
    after_by_id = {int(item["id"]): item for item in after.get("results", [])}
    pairs = []
    equivalent_fields = (
        "status",
        "actual_route",
        "recipe",
        "execution_mode",
        "analysis_execution_mode",
        "dataset_keys",
        "row_count",
    )
    for case_id in sorted(set(before_by_id) | set(after_by_id)):
        lhs = before_by_id.get(case_id, {})
        rhs = after_by_id.get(case_id, {})
        differences = {
            key: {"before": lhs.get(key), "after": rhs.get(key)}
            for key in equivalent_fields
            if lhs.get(key) != rhs.get(key)
        }
        pairs.append(
            {
                "id": case_id,
                "equivalent": not differences,
                "differences": differences,
            }
        )

    before_summary = before.get("summary", {})
    after_summary = after.get("summary", {})
    before_latency = float(before_summary.get("intent_median_total_latency_ms") or 0)
    after_latency = float(after_summary.get("intent_median_total_latency_ms") or 0)
    before_ttft = float(before_summary.get("intent_median_ttft_ms") or 0)
    after_ttft = float(after_summary.get("intent_median_ttft_ms") or 0)
    return {
        "all_cases_equivalent": all(item["equivalent"] for item in pairs),
        "equivalent_cases": sum(item["equivalent"] for item in pairs),
        "different_cases": sum(not item["equivalent"] for item in pairs),
        "pairs": pairs,
        "intent_median_total_latency_delta_ms": round(after_latency - before_latency, 3),
        "intent_median_total_latency_change_pct": round(
            ((after_latency - before_latency) / before_latency) * 100,
            3,
        ) if before_latency else None,
        "intent_median_ttft_delta_ms": round(after_ttft - before_ttft, 3),
        "intent_median_ttft_change_pct": round(
            ((after_ttft - before_ttft) / before_ttft) * 100,
            3,
        ) if before_ttft else None,
        "intent_cached_content_token_delta": (
            _non_negative_int(after_summary.get("intent_cached_content_tokens"))
            - _non_negative_int(before_summary.get("intent_cached_content_tokens"))
        ),
    }


def _resolve_cases(ids: str) -> list[dict[str, Any]]:
    cases = list(base.representative_cases())
    if ids.strip().lower() == "all":
        return cases
    selected = {
        int(value.strip())
        for value in (ids or ",".join(map(str, DEFAULT_CASE_IDS))).split(",")
        if value.strip()
    }
    resolved = [item for item in cases if int(item["id"]) in selected]
    missing = selected - {int(item["id"]) for item in resolved}
    if missing:
        raise ValueError(f"unknown representative case ids: {sorted(missing)}")
    return resolved


def _load_template_bundle(directory_text: str) -> tuple[Path, dict[str, str]]:
    directory = Path(directory_text)
    if not directory.is_absolute():
        directory = ROOT / directory
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    templates: dict[str, str] = {}
    missing: list[str] = []
    for stage, filename in TEMPLATE_FILES.items():
        path = directory / filename
        if not path.is_file():
            missing.append(filename)
            continue
        templates[stage] = path.read_text(encoding="utf-8")
    if missing:
        raise FileNotFoundError(
            f"template bundle {directory} is missing: {', '.join(missing)}"
        )
    return directory, templates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two four-template prompt layouts with live Gemini representative validation."
    )
    parser.add_argument("--before-dir", required=True)
    parser.add_argument("--after-dir", required=True)
    parser.add_argument("--ids", default=",".join(map(str, DEFAULT_CASE_IDS)))
    parser.add_argument("--reference-date", default="20260701")
    parser.add_argument("--output", default="artifacts/prompt_cache_ab/live_benchmark.json")
    args = parser.parse_args()

    base.load_dotenv(ROOT / ".env")
    base.install_lfx_stubs()
    before_path, before_templates = _load_template_bundle(args.before_dir)
    after_path, after_templates = _load_template_bundle(args.after_dir)
    unchanged = [
        stage for stage in TEMPLATE_FILES
        if before_templates[stage] == after_templates[stage]
    ]
    if unchanged:
        raise ValueError(f"before and after templates are identical for stages: {unchanged}")
    field_mismatches = {
        stage: {
            "before": template_format_fields(before_templates[stage]),
            "after": template_format_fields(after_templates[stage]),
        }
        for stage in TEMPLATE_FILES
        if template_format_fields(before_templates[stage])
        != template_format_fields(after_templates[stage])
    }
    if field_mismatches:
        raise ValueError(f"template variable contracts differ: {field_mismatches}")

    modules = base.load_flow_modules()
    v2_modules = routes._v2_modules()
    metadata_context = base.load_metadata_context(modules)
    llm_config = base.resolve_llm_config()
    manifest = routes.load_route_manifest()
    cases = _resolve_cases(args.ids)
    missing = [int(item["id"]) for item in cases if int(item["id"]) not in manifest]
    if missing:
        raise ValueError(f"route manifest is missing case ids: {missing}")

    common = {
        "cases": cases,
        "manifest": manifest,
        "modules": modules,
        "v2_modules": v2_modules,
        "metadata_context": metadata_context,
        "llm_config": llm_config,
        "reference_date": args.reference_date,
    }
    before = run_variant(name="before", templates=before_templates, **common)
    after = run_variant(name="after", templates=after_templates, **common)
    report = {
        "status": (
            "ok"
            if before["summary"]["failed"] == 0
            and after["summary"]["failed"] == 0
            and compare_variants(before, after)["all_cases_equivalent"]
            else "error"
        ),
        "benchmark_contract": {
            "only_changed_inputs": list(TEMPLATE_FILES.values()),
            "provider": str(os.getenv("LLM_PROVIDER") or "gemini"),
            "model": str(llm_config["model"]),
            "reference_date": args.reference_date,
            "case_ids": [int(item["id"]) for item in cases],
            "before_template_dir": str(before_path.relative_to(ROOT)) if before_path.is_relative_to(ROOT) else before_path.name,
            "after_template_dir": str(after_path.relative_to(ROOT)) if after_path.is_relative_to(ROOT) else after_path.name,
            "measurement_note": (
                "TTFT is time to the first non-empty Gemini SSE content event. "
                "cachedContentTokenCount is provider-reported implicit/explicit cache reuse; "
                "zero can mean no hit or an unavailable field."
            ),
        },
        "before": before,
        "after": after,
    }
    report["comparison"] = compare_variants(before, after)

    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "output": str(output),
        "before": before["summary"],
        "after": after["summary"],
        "comparison": report["comparison"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
