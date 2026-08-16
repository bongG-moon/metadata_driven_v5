from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_PREFIX = "요청별 입력 경계:"
PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")

TEMPLATES = {
    "intent": {
        "path": ROOT / "validation_artifacts/prompt_cache_ab/after/templates/03_intent_prompt_template_ko.md",
        "legacy_lines_sha256": "1af3c0d5a9fa9191082c9519e2d87c05fbb3a38f130559e3a525511bcfa0b689",
        "placeholders": [
            "output_schema",
            "metadata_candidates",
            "specialized_prompt",
            "state_summary",
            "question",
        ],
    },
    "pandas": {
        "path": ROOT / "validation_artifacts/prompt_cache_ab/after/templates/16_pandas_prompt_template_ko.md",
        "legacy_lines_sha256": "149664e89f88f5de8d0fea07c6c42a3a5e62bd5107d9b1ecb42ffc8d697ca4bc",
        "placeholders": [
            "intent_plan_json",
            "source_schema_json",
            "source_preview_json",
            "function_case_selection_json",
            "function_case_helper_code",
            "output_contract_json",
        ],
    },
    "answer": {
        "path": ROOT / "validation_artifacts/prompt_cache_ab/after/templates/19_answer_prompt_template_ko.md",
        "legacy_lines_sha256": "b1f458afd36d08a011b6f6c83d31ba0cc8576b8caf186b7f9ed83a58bbf92d53",
        "placeholders": [
            "result_summary_json",
            "applied_scope_json",
            "answer_context_json",
            "domain_answer_guidance",
            "warnings_errors_json",
            "question",
        ],
    },
    "pandas_repair": {
        "path": ROOT / "validation_artifacts/prompt_cache_ab/after/templates/17b_pandas_repair_prompt_template_ko.md",
        "legacy_lines_sha256": "a793cfcce8ac74a7046f29127e6c644bb8e5e65faffa3d6b808a021ffd2e8add",
        "placeholders": [
            "repair_required",
            "intent_plan_json",
            "source_schema_json",
            "source_preview_json",
            "failed_code",
            "error_context_json",
            "function_case_selection_json",
            "function_case_helper_code",
            "output_schema",
        ],
    },
}


def _read(config: dict[str, object]) -> str:
    return config["path"].read_text(encoding="utf-8")


def _legacy_line_multiset_hash(text: str) -> str:
    """Fingerprint all pre-existing nonblank lines independent of their order."""
    legacy_lines = sorted(
        line
        for line in text.splitlines()
        if line.strip() and not line.startswith(BOUNDARY_PREFIX)
    )
    return hashlib.sha256("\n".join(legacy_lines).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", TEMPLATES)
def test_reordered_templates_preserve_every_legacy_nonblank_line(name: str) -> None:
    config = TEMPLATES[name]
    assert _legacy_line_multiset_hash(_read(config)) == config["legacy_lines_sha256"]


@pytest.mark.parametrize("name", TEMPLATES)
def test_dynamic_placeholders_are_confined_to_cache_friendly_tail(name: str) -> None:
    config = TEMPLATES[name]
    text = _read(config)
    matches = list(PLACEHOLDER_RE.finditer(text))

    assert [match.group(1) for match in matches] == config["placeholders"]
    assert text.count(BOUNDARY_PREFIX) == 1
    boundary_index = text.index(BOUNDARY_PREFIX)
    assert boundary_index < matches[0].start()
    assert not PLACEHOLDER_RE.search(text[:boundary_index])
    assert matches[0].start() / len(text) >= 0.90


@pytest.mark.parametrize("name", TEMPLATES)
def test_reordered_templates_still_render_with_their_declared_variables(name: str) -> None:
    config = TEMPLATES[name]
    text = _read(config)
    variables = config["placeholders"]
    rendered = text.format(**{variable: f"VALUE_{variable}" for variable in variables})

    assert not PLACEHOLDER_RE.search(rendered)
    for variable in variables:
        assert f"VALUE_{variable}" in rendered


@pytest.mark.parametrize("name", ["intent", "answer"])
def test_user_question_is_the_last_dynamic_input(name: str) -> None:
    text = _read(TEMPLATES[name]).rstrip()

    assert PLACEHOLDER_RE.findall(text)[-1] == "question"
    assert text.endswith('- 사용자 질문: `{question}`')
