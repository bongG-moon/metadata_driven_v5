from __future__ import annotations

import json

from tools import benchmark_prompt_cache_speed_probe as probe


def test_alternating_order_crosses_ab_and_ba():
    assert probe.alternating_order(0) == ("before", "after")
    assert probe.alternating_order(1) == ("after", "before")
    assert probe.alternating_order(2) == ("before", "after")


def test_reordered_template_has_longer_common_prefix_when_only_question_changes():
    before = (
        "stable header\nquestion={question}\nmetadata={metadata_candidates}\n"
        "stable long rules " + ("R" * 1000)
    )
    after = (
        "stable header\nstable long rules " + ("R" * 1000)
        + "\nmetadata={metadata_candidates}\nquestion={question}"
    )
    common = {
        "metadata_candidates": "M" * 500,
        "state_summary": "{}",
        "output_schema": "{}",
        "specialized_prompt": "fixed",
    }
    prompts = {}
    for name, template in {"before": before, "after": after}.items():
        prompts[name] = [
            probe.render_probe_prompt(
                template,
                common_variables=common,
                question=question,
                namespace="namespace",
            )
            for question in ("A-first", "B-second")
        ]

    assert probe.common_prefix_chars(*prompts["after"]) > probe.common_prefix_chars(
        *prompts["before"]
    )


def test_call_gemini_probe_sends_output_cap_without_sampling_and_discards_text():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            yield (
                b'data: {"candidates":[{"content":{"parts":[{"text":"{\\"ok\\":true}"}]}}],'
                b'"usageMetadata":{"promptTokenCount":3000,"cachedContentTokenCount":2000}}\n'
            )

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    ticks = iter((10.0, 10.2, 10.7))
    result = probe.call_gemini_probe(
        "raw-prompt-must-not-be-returned",
        {"model": "probe-model", "api_key": "super-secret", "timeout": 9},
        max_output_tokens=64,
        clock=lambda: next(ticks),
        urlopen=fake_urlopen,
    )

    generation = captured["body"]["generationConfig"]
    assert generation == {
        "maxOutputTokens": 64,
        "responseMimeType": "application/json",
    }
    assert "temperature" not in generation
    assert captured["timeout"] == 9
    assert result["ttft_ms"] == 200.0
    assert result["total_latency_ms"] == 700.0
    assert result["usage"]["cachedContentTokenCount"] == 2000
    assert result["cachedContentTokenCount_present"] is True
    assert result["raw_usage_keys"] == [
        "cachedContentTokenCount",
        "promptTokenCount",
    ]
    serialized = json.dumps(result)
    assert "raw-prompt-must-not-be-returned" not in serialized
    assert "super-secret" not in serialized
    assert '{"ok":true}' not in serialized


def test_call_gemini_probe_distinguishes_missing_raw_cached_token_field():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            yield (
                b'data: {"candidates":[{"content":{"parts":[{"text":"{}"}]}}],'
                b'"usageMetadata":{"promptTokenCount":3000,"totalTokenCount":3002}}\n'
            )

    ticks = iter((20.0, 20.1, 20.3))
    result = probe.call_gemini_probe(
        "probe",
        {"model": "probe-model", "api_key": "secret", "timeout": 9},
        max_output_tokens=64,
        clock=lambda: next(ticks),
        urlopen=lambda request, timeout: FakeResponse(),
    )

    assert result["usage"]["cachedContentTokenCount"] == 0
    assert result["cachedContentTokenCount_present"] is False
    assert result["raw_usage_keys"] == ["promptTokenCount", "totalTokenCount"]


def test_run_phase_uses_same_hidden_question_for_each_ab_pair():
    seen = []

    def fake_caller(prompt, config, *, max_output_tokens):
        seen.append(prompt)
        return {
            "ttft_ms": 10,
            "total_latency_ms": 20,
            "prompt_chars": len(prompt),
            "prompt_sha256": probe._sha256_text(prompt),
            "content_received": True,
            "raw_usage_keys": ["promptTokenCount"],
            "cachedContentTokenCount_present": False,
            "usage": probe.ab.normalize_usage_metadata({"promptTokenCount": 10}),
        }

    templates = {
        "before": "B|{question}|{metadata_candidates}",
        "after": "A|{metadata_candidates}|{question}",
    }
    common = probe.synthetic_common_variables()
    records = probe.run_phase(
        templates=templates,
        common_variables=common,
        namespace="ns",
        questions=["hidden-1", "hidden-2"],
        config={},
        max_output_tokens=64,
        caller=fake_caller,
    )

    assert [(item["pair_index"], item["variant"]) for item in records] == [
        (0, "before"),
        (0, "after"),
        (1, "after"),
        (1, "before"),
    ]
    assert all("hidden-1" in prompt for prompt in seen[:2])
    assert all("hidden-2" in prompt for prompt in seen[2:])
    assert all("prompt" not in item for item in records)


def test_summary_separates_first_call_and_interpretation_requires_hit_and_ttft():
    def measurement(variant, pair, ttft, cached):
        return {
            "variant": variant,
            "pair_index": pair,
            "ttft_ms": ttft,
            "total_latency_ms": ttft + 50,
            "usage": probe.ab.normalize_usage_metadata(
                {
                    "promptTokenCount": 4000,
                    "cachedContentTokenCount": cached,
                }
            ),
            "cachedContentTokenCount_present": True,
        }

    exact = [
        measurement("before", 0, 500, 0),
        measurement("after", 0, 500, 0),
        measurement("after", 1, 300, 3000),
        measurement("before", 1, 350, 3000),
    ]
    suffix = [
        measurement("before", 0, 500, 0),
        measurement("after", 0, 500, 0),
        measurement("after", 1, 250, 3000),
        measurement("before", 1, 450, 0),
    ]
    exact_summary = probe.summarize_phase(exact)
    suffix_summary = probe.summarize_phase(suffix)
    interpretation = probe._interpret(exact_summary, suffix_summary)

    assert exact_summary["before"]["after_first_call"]["call_count"] == 1
    assert (
        exact_summary["before"]["after_first_call"][
            "cached_content_token_field_present_calls"
        ]
        == 1
    )
    assert interpretation["provider_cache_observed_on_exact_repeat"] is True
    assert interpretation["provider_cache_observation_status"] == "cache_hit_observed"
    assert interpretation["cached_content_token_field_present_calls"] == 8
    assert interpretation["measured_calls"] == 8
    assert interpretation["after_suffix_cache_hits_exceed_before"] is True
    assert interpretation["after_suffix_median_ttft_is_lower"] is True
    assert interpretation["speed_improvement_supported"] is True
