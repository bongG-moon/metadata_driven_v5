from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = ROOT / "langflow_components" / "route_flow_v2" / "02_agent_direct_tool_result_adapter.py"


def _load_component():
    spec = importlib.util.spec_from_file_location("test_agent_direct_tool_result_adapter", COMPONENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _ToolBlock:
    name: str = "run_data_analysis"
    type: str = "tool_use"
    output: Any = None
    error: Any = None
    contents: list[Any] = field(default_factory=list)


@dataclass
class _AgentMessage:
    text: str = ""
    content_blocks: list[Any] = field(default_factory=list)
    session_id: str = "session-1"
    context_id: str = ""


def test_prefers_successful_tool_content_over_nested_child_llm_json() -> None:
    module = _load_component()
    child_answer = "### 답변\n요청한 생산량은 6건입니다."
    agent = _AgentMessage(
        text='{"intent_plan":{"analysis_kind":"production_quantity_by_process"}}',
        content_blocks=[_ToolBlock(output={"content": child_answer, "status": "success"})],
    )

    result = module.build_direct_tool_result(agent)

    assert result.text == child_answer
    assert result.session_id == "session-1"
    assert "intent_plan" not in result.text


def test_reads_nested_tool_content_and_ignores_earlier_tool_result() -> None:
    module = _load_component()
    agent = _AgentMessage(
        content_blocks=[
            _ToolBlock(output={"content": "이전 결과", "status": "success"}),
            {"type": "content_block", "contents": [_ToolBlock(output={"data": {"content": "최종 결과"}})]},
        ]
    )

    result = module.build_direct_tool_result(agent)

    assert result.text == "최종 결과"


def test_tool_without_displayable_result_never_falls_back_to_raw_agent_json() -> None:
    module = _load_component()
    agent = _AgentMessage(
        text='{"intent_plan":{"unexpected":"internal child event"}}',
        content_blocks=[_ToolBlock(output=None)],
    )

    result = module.build_direct_tool_result(agent)

    assert result.text == "도구 실행은 완료되었지만 표시할 최종 답변을 받지 못했습니다."
    assert "intent_plan" not in result.text


def test_non_tool_agent_response_remains_available_for_clarification() -> None:
    module = _load_component()
    agent = _AgentMessage(text="어떤 날짜를 기준으로 조회할까요?")

    result = module.build_direct_tool_result(agent)

    assert result.text == "어떤 날짜를 기준으로 조회할까요?"
