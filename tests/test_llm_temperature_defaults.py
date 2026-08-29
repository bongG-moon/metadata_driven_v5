"""Regression coverage for the Flow-wide deterministic LLM temperature policy."""

from __future__ import annotations

import json
from pathlib import Path

from component_test_support import ROOT


FLOW_EXPORTS = ROOT / "flow_exports"


def _flow(filename: str) -> dict:
    return json.loads((FLOW_EXPORTS / filename).read_text(encoding="utf-8"))


def _node(flow: dict, node_id: str) -> dict:
    return next(node for node in flow["data"]["nodes"] if node["id"] == node_id)


def _source(flow: dict, node_id: str) -> str:
    template = _node(flow, node_id)["data"]["node"]["template"]
    return str(template.get("code", {}).get("value") or "")


def test_every_runtime_llm_path_defaults_to_zero_temperature():
    """Check every current runtime LLM path, including custom `get_llm` calls."""

    native_language_models = {
        "domain_saving_flow_v5_standalone.json": "LanguageModelExtract-domain",
        "table_catalog_saving_flow_v5_standalone.json": "LanguageModelExtract-table_catalog",
        "main_flow_filter_saving_flow_v5_standalone.json": "LanguageModelExtract-main_flow_filter",
        "metadata_qa_flow_v5_standalone.json": "LanguageModel-metadata-qa",
        "07_realtime_production_report_legacy_flow_v5_standalone.json": (
            "LanguageModelProcessGroup-realtime-production-report-legacy"
        ),
    }
    for filename, node_id in native_language_models.items():
        template = _node(_flow(filename), node_id)["data"]["node"]["template"]
        assert float(template["temperature"]["value"]) == 0.0

    data_analysis = _flow("data_analysis_flow_v2_standalone.json")
    for node_id in (
        "LanguageModel-intent",
        "CustomComponent-s3mf1",
        "CustomComponent-BVItv",
    ):
        source = _source(data_analysis, node_id)
        assert "LLM_TEMPERATURE = 0.0" in source
        assert "temperature=LLM_TEMPERATURE" in source

    router = _flow("06_agent_tool_router_flow_v5_standalone.json")
    router_source = _source(router, "Agent-agent-tool-router")
    assert "LLM_TEMPERATURE = 0.0" in router_source
    assert "temperature=LLM_TEMPERATURE" in router_source

    report_followup = _flow("07_2_report_followup_flow_v5_standalone.json")
    report_template = _node(
        report_followup,
        "GuardedPlanRouter-report-followup",
    )["data"]["node"]["template"]
    assert float(report_template["temperature"]["value"]) == 0.0
    assert "temperature=getattr(self, \"temperature\", LLM_TEMPERATURE)" in _source(
        report_followup,
        "GuardedPlanRouter-report-followup",
    )
