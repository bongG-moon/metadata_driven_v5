from __future__ import annotations

import importlib.util
from pathlib import Path

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = (
    ROOT
    / "langflow_components"
    / "route_flow_v2"
    / "01_cached_named_run_flow_tool.py"
)


def _load_component():
    spec = importlib.util.spec_from_file_location(
        "test_cached_named_run_flow_tool_component",
        COMPONENT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _QuestionInput(BaseModel):
    question: str


def _tool_call(name: str, args: dict) -> dict:
    return {
        "name": name,
        "args": args,
        "id": f"call-{name}",
        "type": "tool_call",
    }


def test_safe_tool_validation_error_keeps_lfx_error_status_without_running_child() -> None:
    module = _load_component()
    calls = 0

    def child(question: str) -> str:
        nonlocal calls
        calls += 1
        return question

    tool = StructuredTool.from_function(
        func=child,
        name="run_data_analysis",
        description="test",
        args_schema=_QuestionInput,
    )
    module._configure_safe_tool_errors(tool)

    result = tool.invoke(_tool_call(tool.name, {}))

    assert calls == 0
    assert result.status == "error"
    assert result.content == module.TOOL_INPUT_VALIDATION_ERROR_MESSAGE
    assert "validation" not in str(result.content).casefold()


def test_safe_tool_runtime_error_keeps_lfx_error_status_and_redacts_details() -> None:
    module = _load_component()
    calls = 0
    secret = "mongodb://user:password@example.invalid/db flow_id=secret-flow"

    def child(question: str) -> str:
        nonlocal calls
        calls += 1
        raise ToolException(secret)

    tool = StructuredTool.from_function(
        func=child,
        name="save_domain_metadata",
        description="test",
        args_schema=_QuestionInput,
    )
    module._configure_safe_tool_errors(tool)

    result = tool.invoke(_tool_call(tool.name, {"question": "등록해줘"}))

    assert calls == 1
    assert result.status == "error"
    assert result.content == module.TOOL_RUNTIME_ERROR_MESSAGE
    assert secret not in str(result.content)
    assert "password" not in str(result.content).casefold()
    assert "secret-flow" not in str(result.content)


def test_safe_tool_handlers_do_not_change_normal_blocked_or_clarification_results() -> None:
    module = _load_component()
    clarification = "### 공정그룹을 선택해 주세요.\n\nReport는 아직 생성하지 않았습니다."

    def child(question: str) -> str:
        del question
        return clarification

    tool = StructuredTool.from_function(
        func=child,
        name="run_realtime_production_report",
        description="test",
        args_schema=_QuestionInput,
        return_direct=True,
    )
    module._configure_safe_tool_errors(tool)

    result = tool.invoke(_tool_call(tool.name, {"question": "실시간 생산 분석"}))

    assert result.status == "success"
    assert result.content == clarification
    assert tool.return_direct is True
