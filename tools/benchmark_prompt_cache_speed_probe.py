#!/usr/bin/env python3
"""Measure Gemini prompt-cache behavior without running the full analysis Flow.

This probe isolates the model/API prefill path from retrieval, pandas execution,
answer generation, and planner-output correctness.  It uses the Intent templates
from the preserved Before/After bundles and runs two experiments:

1. exact-repeat: the same completed prompt is sent repeatedly, which verifies
   whether provider-side caching is observable at all;
2. varying-suffix: metadata, state, schema, and all other values remain fixed,
   while only the question changes, which compares the reusable prefix created
   by the two template layouts.

Warm-up requests are separate and excluded.  Measured pairs alternate AB/BA to
reduce ordering bias.  The report never stores API keys, prompt/response text,
or the generated question values; it contains hashes, sizes, timing, and usage
counters only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import benchmark_prompt_cache_ab as ab  # noqa: E402
from tools import validate_representative_questions as base  # noqa: E402


DEFAULT_BEFORE_DIR = "validation_artifacts/prompt_cache_ab/before/templates"
DEFAULT_AFTER_DIR = "validation_artifacts/prompt_cache_ab/after/templates"
VARIANTS = ("before", "after")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _median(values: Iterable[float | int | None]) -> float | None:
    resolved = [float(value) for value in values if value is not None]
    return round(float(statistics.median(resolved)), 3) if resolved else None


def alternating_order(pair_index: int) -> tuple[str, str]:
    """Return AB for even pairs and BA for odd pairs."""

    return VARIANTS if pair_index % 2 == 0 else tuple(reversed(VARIANTS))


def common_prefix_chars(left: str, right: str) -> int:
    """Count identical leading characters without retaining either input."""

    for index, (lhs, rhs) in enumerate(zip(left, right)):
        if lhs != rhs:
            return index
    return min(len(left), len(right))


def synthetic_common_variables() -> dict[str, str]:
    """Build stable, non-production context large enough for a cache probe."""

    catalog_items = []
    for index in range(24):
        catalog_items.append(
            {
                "dataset_key": f"probe_dataset_{index:02d}",
                "display_name": f"Probe Dataset {index:02d}",
                "columns": ["DATE", "OPER_NAME", "PRODUCT", "QUANTITY"],
                "metric_columns": ["QUANTITY"],
                "use_when": (
                    "속도 측정용 고정 카탈로그 항목이며 실제 데이터 조회에는 사용하지 않는다. "
                    "동일한 metadata 접두부 길이를 확보하기 위한 비민감 합성 설명이다."
                ),
                "exclude_when": "실제 업무 분석 또는 외부 데이터 조회가 필요한 경우",
            }
        )
    metadata = {
        "table_catalog_items": catalog_items,
        "domain_metadata": {
            "quantity_terms": ["QUANTITY"],
            "process_groups": ["PROBE_A", "PROBE_B"],
        },
        "runtime_function_helpers": [],
    }
    state = {
        "request_context": {
            "reference_date": "20260701",
            "previous_date": "20260630",
            "followup_hint": False,
        },
        "current_data": {},
        "previous_result_schema": [],
    }
    schema = {
        "type": "object",
        "required": ["intent_plan"],
        "properties": {
            "intent_plan": {
                "type": "object",
                "required": ["analysis_kind", "retrieval_jobs", "pandas_execution_plan"],
                "properties": {
                    "analysis_kind": {"type": "string"},
                    "retrieval_jobs": {"type": "array"},
                    "pandas_execution_plan": {"type": "array"},
                },
            }
        },
    }
    return {
        "metadata_candidates": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        "state_summary": json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        "output_schema": json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "specialized_prompt": "속도 측정용 합성 입력이다. 실제 데이터 조회를 수행하지 않는다.",
    }


def render_probe_prompt(
    template: str,
    *,
    common_variables: dict[str, str],
    question: str,
    namespace: str,
) -> str:
    """Render one template and prepend a per-experiment cache namespace."""

    values = dict(common_variables)
    values["question"] = question
    rendered = template.format(**values)
    return f"prompt-cache-speed-probe namespace={namespace}\n{rendered}"


def _extract_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            return str(payload.get("error", {}).get("message") or "").strip()[:500]
    except Exception:
        pass
    return ""


def call_gemini_probe(
    prompt: str,
    config: dict[str, Any],
    *,
    max_output_tokens: int,
    clock: Callable[[], float] = time.perf_counter,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Call Gemini SSE once and discard content after timing its first token.

    Gemini 3.5 Flash-Lite ignores temperature, so this controlled probe sends no
    sampling parameters.  ``maxOutputTokens`` bounds generation work while the
    SSE stream still exposes TTFT and final ``usageMetadata``.
    """

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
            "maxOutputTokens": int(max_output_tokens),
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
    usage: dict[str, Any] = {}
    content_chars = 0
    try:
        with urlopen(request, timeout=config["timeout"]) as response:
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
                text_delta, event_usage = ab._extract_stream_payload(event)
                if text_delta:
                    if first_content_at is None:
                        first_content_at = clock()
                    content_chars += len(text_delta)
                if event_usage:
                    usage = event_usage
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error(exc)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Gemini request failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc

    finished = clock()
    if first_content_at is None:
        raise RuntimeError(
            "Gemini probe response had no text; increase --max-output-tokens so TTFT remains measurable"
        )
    return {
        "ttft_ms": round((first_content_at - started) * 1000, 3),
        "total_latency_ms": round((finished - started) * 1000, 3),
        "prompt_chars": len(prompt),
        "prompt_sha256": _sha256_text(prompt),
        "content_received": content_chars > 0,
        # Preserve only raw provider key names, never raw values.  This keeps
        # "field explicitly reported as zero" distinguishable from "field was
        # absent and normalize_usage_metadata supplied the default zero".
        "raw_usage_keys": sorted(str(key) for key in usage),
        "cachedContentTokenCount_present": "cachedContentTokenCount" in usage,
        "usage": ab.normalize_usage_metadata(usage),
    }


def summarize_measurements(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate timing and provider cache counters for one variant."""

    usage = [item.get("usage", {}) for item in measurements]
    prompt_tokens = sum(int(item.get("promptTokenCount") or 0) for item in usage)
    cached_tokens = sum(int(item.get("cachedContentTokenCount") or 0) for item in usage)
    return {
        "call_count": len(measurements),
        "median_ttft_ms": _median(item.get("ttft_ms") for item in measurements),
        "median_total_latency_ms": _median(
            item.get("total_latency_ms") for item in measurements
        ),
        "prompt_tokens": prompt_tokens,
        "cached_content_tokens": cached_tokens,
        "cache_hit_calls": sum(bool(item.get("cache_hit")) for item in usage),
        "cached_content_token_field_present_calls": sum(
            bool(item.get("cachedContentTokenCount_present")) for item in measurements
        ),
        "cache_read_ratio": round(cached_tokens / prompt_tokens, 6) if prompt_tokens else 0.0,
    }


def summarize_phase(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, Any] = {}
    for variant in VARIANTS:
        selected = [item for item in records if item.get("variant") == variant]
        by_variant[variant] = {
            "all_calls": summarize_measurements(selected),
            "after_first_call": summarize_measurements(selected[1:]),
        }
    return by_variant


def _safe_measurement(
    measurement: dict[str, Any],
    *,
    variant: str,
    pair_index: int,
    order_position: int,
) -> dict[str, Any]:
    """Attach non-sensitive schedule labels to one measurement."""

    result = dict(measurement)
    result.update(
        {
            "variant": variant,
            "pair_index": pair_index,
            "order": "AB" if pair_index % 2 == 0 else "BA",
            "order_position": order_position,
        }
    )
    return result


def run_phase(
    *,
    templates: dict[str, str],
    common_variables: dict[str, str],
    namespace: str,
    questions: list[str],
    config: dict[str, Any],
    max_output_tokens: int,
    caller: Callable[..., dict[str, Any]] = call_gemini_probe,
) -> list[dict[str, Any]]:
    """Execute AB/BA pairs; each pair uses one hidden question for both layouts."""

    records: list[dict[str, Any]] = []
    for pair_index, question in enumerate(questions):
        prompts = {
            variant: render_probe_prompt(
                templates[variant],
                common_variables=common_variables,
                question=question,
                namespace=namespace,
            )
            for variant in VARIANTS
        }
        for order_position, variant in enumerate(alternating_order(pair_index)):
            measurement = caller(
                prompts[variant],
                config,
                max_output_tokens=max_output_tokens,
            )
            records.append(
                _safe_measurement(
                    measurement,
                    variant=variant,
                    pair_index=pair_index,
                    order_position=order_position,
                )
            )
    return records


def _template_manifest(
    before_bundle: dict[str, str],
    after_bundle: dict[str, str],
) -> dict[str, Any]:
    return {
        stage: {
            "before_sha256": _sha256_text(before_bundle[stage]),
            "after_sha256": _sha256_text(after_bundle[stage]),
            "before_chars": len(before_bundle[stage]),
            "after_chars": len(after_bundle[stage]),
            "format_fields_equal": (
                ab.template_format_fields(before_bundle[stage])
                == ab.template_format_fields(after_bundle[stage])
            ),
        }
        for stage in ab.TEMPLATE_FILES
    }


def _interpret(
    exact_summary: dict[str, Any],
    suffix_summary: dict[str, Any],
) -> dict[str, Any]:
    exact_hits = sum(
        int(exact_summary[variant]["after_first_call"]["cache_hit_calls"])
        for variant in VARIANTS
    )
    before_suffix = suffix_summary["before"]["after_first_call"]
    after_suffix = suffix_summary["after"]["after_first_call"]
    field_present_calls = sum(
        int(exact_summary[variant]["all_calls"]["cached_content_token_field_present_calls"])
        + int(suffix_summary[variant]["all_calls"]["cached_content_token_field_present_calls"])
        for variant in VARIANTS
    )
    measured_calls = sum(
        int(exact_summary[variant]["all_calls"]["call_count"])
        + int(suffix_summary[variant]["all_calls"]["call_count"])
        for variant in VARIANTS
    )
    before_ttft = before_suffix.get("median_ttft_ms")
    after_ttft = after_suffix.get("median_ttft_ms")
    return {
        "cached_content_token_field_present_calls": field_present_calls,
        "measured_calls": measured_calls,
        "provider_cache_observation_status": (
            "cache_hit_observed"
            if exact_hits > 0
            else "field_reported_without_cache_hit"
            if field_present_calls > 0
            else "cached_content_token_field_not_reported"
        ),
        "provider_cache_observed_on_exact_repeat": exact_hits > 0,
        "after_suffix_cache_hits_exceed_before": (
            int(after_suffix.get("cache_hit_calls") or 0)
            > int(before_suffix.get("cache_hit_calls") or 0)
        ),
        "after_suffix_median_ttft_is_lower": (
            after_ttft is not None and before_ttft is not None and after_ttft < before_ttft
        ),
        "speed_improvement_supported": (
            int(after_suffix.get("cache_hit_calls") or 0)
            > int(before_suffix.get("cache_hit_calls") or 0)
            and after_ttft is not None
            and before_ttft is not None
            and after_ttft < before_ttft
        ),
        "note": (
            "A speed claim is supported only when the reordered suffix sequence has both "
            "more provider-reported cache-hit calls and lower median TTFT. Total latency is "
            "secondary because it also includes bounded output generation. If the raw "
            "cachedContentTokenCount field is absent, normalized zero is not evidence that "
            "the provider explicitly reported zero cached tokens."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an isolated exact-repeat and varying-question Prompt Cache speed probe."
    )
    parser.add_argument("--before-dir", default=DEFAULT_BEFORE_DIR)
    parser.add_argument("--after-dir", default=DEFAULT_AFTER_DIR)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--warmup-calls", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument(
        "--output",
        default="validation_artifacts/prompt_cache_ab/cache_speed_probe.json",
    )
    args = parser.parse_args()
    if args.repeats < 2:
        raise ValueError("--repeats must be at least 2")
    if args.warmup_calls < 0:
        raise ValueError("--warmup-calls cannot be negative")
    if args.max_output_tokens < 16:
        raise ValueError("--max-output-tokens must be at least 16")

    base.load_dotenv(ROOT / ".env")
    before_path, before_bundle = ab._load_template_bundle(args.before_dir)
    after_path, after_bundle = ab._load_template_bundle(args.after_dir)
    template_manifest = _template_manifest(before_bundle, after_bundle)
    mismatches = [
        stage for stage, item in template_manifest.items()
        if not item["format_fields_equal"]
    ]
    if mismatches:
        raise ValueError(f"template variable contracts differ: {mismatches}")
    if before_bundle["intent"] == after_bundle["intent"]:
        raise ValueError("before and after Intent templates are identical")

    config = base.resolve_llm_config()
    common_variables = synthetic_common_variables()

    # These requests warm the endpoint/model only. Unique, short prompts avoid
    # populating either measured template prefix, and their timings are excluded.
    warmup_measurements = []
    for _ in range(args.warmup_calls):
        warmup_prompt = (
            f"prompt-cache-speed-probe-warmup={uuid.uuid4().hex}\n"
            "JSON으로 {\"ok\":true}만 반환하라."
        )
        warmup_measurements.append(
            call_gemini_probe(
                warmup_prompt,
                config,
                max_output_tokens=args.max_output_tokens,
            )
        )

    templates = {
        "before": before_bundle["intent"],
        "after": after_bundle["intent"],
    }

    exact_namespace = uuid.uuid4().hex
    exact_question = (
        f"{uuid.uuid4().hex} 속도 측정용 요청이다. "
        "유효한 intent_plan JSON을 가능한 짧게 반환하라."
    )
    exact_questions = [exact_question] * args.repeats
    exact_records = run_phase(
        templates=templates,
        common_variables=common_variables,
        namespace=exact_namespace,
        questions=exact_questions,
        config=config,
        max_output_tokens=args.max_output_tokens,
    )

    suffix_namespace = uuid.uuid4().hex
    suffix_questions = [
        (
            f"{uuid.uuid4().hex} 속도 측정용 요청 {index:02d}이다. "
            "유효한 intent_plan JSON을 가능한 짧게 반환하라."
        )
        for index in range(args.repeats)
    ]
    suffix_records = run_phase(
        templates=templates,
        common_variables=common_variables,
        namespace=suffix_namespace,
        questions=suffix_questions,
        config=config,
        max_output_tokens=args.max_output_tokens,
    )

    # Structural diagnostic: same values and namespace, only the question differs.
    suffix_prompt_pairs = {
        variant: [
            render_probe_prompt(
                templates[variant],
                common_variables=common_variables,
                question=question,
                namespace=suffix_namespace,
            )
            for question in suffix_questions[:2]
        ]
        for variant in VARIANTS
    }
    prefix_diagnostic = {
        variant: {
            "common_prefix_chars": common_prefix_chars(*suffix_prompt_pairs[variant]),
            "prompt_chars": len(suffix_prompt_pairs[variant][0]),
            "common_prefix_ratio": round(
                common_prefix_chars(*suffix_prompt_pairs[variant])
                / len(suffix_prompt_pairs[variant][0]),
                6,
            ),
        }
        for variant in VARIANTS
    }

    exact_summary = summarize_phase(exact_records)
    suffix_summary = summarize_phase(suffix_records)
    report = {
        "schema_version": "prompt-cache-speed-probe/v1",
        "status": "ok",
        "benchmark_contract": {
            "provider": str(os.getenv("LLM_PROVIDER") or "gemini"),
            "model": str(config["model"]),
            "max_output_tokens": args.max_output_tokens,
            "sampling_parameters_sent": [],
            "repeats_per_variant_per_phase": args.repeats,
            "warmup_calls_excluded": args.warmup_calls,
            "order": "AB/BA alternating pairs",
            "before_template_dir": str(before_path.relative_to(ROOT)),
            "after_template_dir": str(after_path.relative_to(ROOT)),
            "sensitive_data_policy": (
                "No prompt text, generated question, response text, API key, or production data is stored."
            ),
            "measurement_note": (
                "TTFT is the first non-empty Gemini SSE text event. Provider cache reuse is "
                "cachedContentTokenCount from usageMetadata. Warm-up is not included."
            ),
        },
        "template_manifest": template_manifest,
        "namespace_hashes": {
            "exact_repeat": _sha256_text(exact_namespace),
            "varying_suffix": _sha256_text(suffix_namespace),
        },
        "warmup": {
            "excluded": True,
            "summary": summarize_measurements(warmup_measurements),
        },
        "exact_repeat": {
            "summary": exact_summary,
            "measurements": exact_records,
        },
        "varying_suffix": {
            "structural_prefix_diagnostic": prefix_diagnostic,
            "summary": suffix_summary,
            "measurements": suffix_records,
        },
        "interpretation": _interpret(exact_summary, suffix_summary),
    }

    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "warmup": report["warmup"],
                "exact_repeat": exact_summary,
                "varying_suffix": suffix_summary,
                "prefix_diagnostic": prefix_diagnostic,
                "interpretation": report["interpretation"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
