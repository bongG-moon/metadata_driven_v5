from __future__ import annotations

import json

from tools import benchmark_prompt_cache_ab as benchmark


def test_normalize_usage_metadata_keeps_provider_cache_read_count():
    result = benchmark.normalize_usage_metadata(
        {
            "promptTokenCount": 4000,
            "cachedContentTokenCount": 3000,
            "candidatesTokenCount": 100,
            "totalTokenCount": 4100,
            "cacheTokensDetails": [{"modality": "TEXT", "tokenCount": 3000}],
            "secret": "must-not-be-preserved",
        }
    )

    assert result["cachedContentTokenCount"] == 3000
    assert result["cache_hit"] is True
    assert result["cache_read_ratio"] == 0.75
    assert result["cacheTokensDetails"] == [{"modality": "TEXT", "tokenCount": 3000}]
    assert "secret" not in result


def test_extract_stream_payload_supports_text_and_usage_only_events():
    text, usage = benchmark._extract_stream_payload(
        {
            "candidates": [{"content": {"parts": [{"text": "{\"intent"}]}}],
            "usageMetadata": {"promptTokenCount": 12},
        }
    )
    assert text == '{"intent'
    assert usage == {"promptTokenCount": 12}

    text, usage = benchmark._extract_stream_payload(
        {"usageMetadata": {"cachedContentTokenCount": 10}}
    )
    assert text == ""
    assert usage == {"cachedContentTokenCount": 10}


def test_injected_templates_change_only_named_files_and_restore_functions(tmp_path):
    original_call = benchmark.base.call_llm
    original_read_text = benchmark.Path.read_text
    regular = tmp_path / "regular.md"
    regular.write_text("normal={question}", encoding="utf-8")
    templates = {
        stage: f"{stage}={{question}}"
        for stage in benchmark.TEMPLATE_FILES
    }

    def fake_call(prompt, config):
        return json.dumps({"prompt": prompt})

    with benchmark.injected_templates(templates, fake_call):
        rendered = benchmark.base.render_prompt(
            tmp_path / benchmark.INTENT_TEMPLATE_NAME,
            {"question": "Q"},
        )
        assert rendered == "intent=Q"
        pandas_path = tmp_path / benchmark.TEMPLATE_FILES["pandas_generation"]
        assert pandas_path.read_text(encoding="utf-8") == "pandas_generation={question}"
        assert benchmark.base.render_prompt(regular, {"question": "Q"}) == "normal=Q"
        assert benchmark.base.call_llm("P", {}) == '{"prompt": "P"}'

    assert benchmark.Path.read_text is original_read_text
    assert benchmark.base.call_llm is original_call


def test_variant_summary_and_comparison_report_cache_and_latency_delta():
    before_results = [
        {
            "id": 1,
            "status": "ok",
            "actual_route": "fast",
            "recipe": "group_summary",
            "execution_mode": "typed",
            "analysis_execution_mode": "deterministic_fast",
            "dataset_keys": ["production_today"],
            "row_count": 1,
            "wall_latency_ms": 1200,
            "model_measurements": [
                {
                    "stage": "intent",
                    "ttft_ms": 500,
                    "total_latency_ms": 1000,
                    "usage": {
                        "promptTokenCount": 4000,
                        "cachedContentTokenCount": 0,
                        "cache_hit": False,
                    },
                }
            ],
        }
    ]
    after_results = json.loads(json.dumps(before_results))
    after_results[0]["wall_latency_ms"] = 900
    after_results[0]["model_measurements"][0].update(
        {"ttft_ms": 300, "total_latency_ms": 700}
    )
    after_results[0]["model_measurements"][0]["usage"].update(
        {"cachedContentTokenCount": 3000, "cache_hit": True}
    )
    before = {"results": before_results, "summary": benchmark.summarize_variant(before_results)}
    after = {"results": after_results, "summary": benchmark.summarize_variant(after_results)}

    comparison = benchmark.compare_variants(before, after)

    assert comparison["all_cases_equivalent"] is True
    assert comparison["intent_median_total_latency_delta_ms"] == -300
    assert comparison["intent_median_ttft_delta_ms"] == -200
    assert comparison["intent_cached_content_token_delta"] == 3000
    assert after["summary"]["intent_cache_read_ratio"] == 0.75
    assert after["summary"]["stage_summaries"]["pandas_repair"]["call_count"] == 0


def test_load_template_bundle_requires_all_four_files(tmp_path):
    for stage, filename in benchmark.TEMPLATE_FILES.items():
        (tmp_path / filename).write_text(f"template-{stage}", encoding="utf-8")

    directory, templates = benchmark._load_template_bundle(str(tmp_path))

    assert directory == tmp_path
    assert templates == {
        stage: f"template-{stage}" for stage in benchmark.TEMPLATE_FILES
    }


def test_classify_existing_prompt_stages():
    assert benchmark.classify_prompt_stage("너는 제조 데이터 분석 intent planner다.") == "intent"
    assert benchmark.classify_prompt_stage("너는 제조 데이터 분석용 pandas code generator다.") == "pandas_generation"
    assert benchmark.classify_prompt_stage("너는 제조 데이터 분석용 pandas code repair agent다.") == "pandas_repair"
    assert benchmark.classify_prompt_stage("너는 제조 데이터 분석 결과를 한국어로 답변하는 agent다.") == "answer_generation"


def test_template_format_fields_ignores_escaped_json_braces():
    template = 'schema={output_schema}\ninput={question}\nexample={{"ok": true}}'

    assert benchmark.template_format_fields(template) == ["output_schema", "question"]
