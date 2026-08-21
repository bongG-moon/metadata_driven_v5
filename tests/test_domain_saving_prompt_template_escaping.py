from __future__ import annotations

from pathlib import Path

from langchain_core.prompts import PromptTemplate
from lfx.base.prompts.api_utils import validate_prompt


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "langflow_components" / "domain_saving_flow" / "03_saving_prompt_template_ko.md"


def test_domain_saving_prompt_treats_derived_metric_json_as_literal_text():
    """Langflow f-string prompts require doubled braces around literal JSON."""

    template = PROMPT_PATH.read_text(encoding="utf-8")

    input_variables = validate_prompt(template)

    assert input_variables == ["source_text"]
    rendered = PromptTemplate(template=template, input_variables=input_variables).format(source_text="보유 CAPA 규칙")
    assert '`{"column":"..."}`' in rendered
    assert '`{"constant":숫자}`' in rendered
    assert 'operands=`[{"column":"A"},{"column":"B"},{"constant":24}]`' in rendered
