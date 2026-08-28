from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = ROOT / "langflow_components" / "route_flow_v2" / "03_silent_direct_return_router_agent.py"


def _load_component():
    spec = importlib.util.spec_from_file_location("test_silent_direct_return_router_agent", COMPONENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_silent_router_agent_send_message_returns_the_original_message_without_side_effects() -> None:
    module = _load_component()
    marker = object()

    result = asyncio.run(
        module.SilentDirectReturnRouterAgent.send_message(
            object(),
            marker,
            id_="ignored",
            skip_db_update=True,
        )
    )

    assert result is marker


def test_tool_content_detection_accepts_runtime_compatible_tool_blocks() -> None:
    module = _load_component()

    class ToolBlock:
        type = "tool_use"
        name = "run_data_analysis"
        output = {"content": "답변"}

    class MessageLike:
        content_blocks = [ToolBlock()]

    assert module._has_tool_content(MessageLike()) is True


def test_router_agent_includes_the_langflow_1_11_calculator_compatibility_input() -> None:
    module = _load_component()

    calculator = next(
        item for item in module.SilentDirectReturnRouterAgent.inputs if item.name == "add_calculator_tool"
    )

    assert calculator.value is False


def test_router_context_uses_one_completed_exchange_and_selected_flow_in_system_prompt() -> None:
    module = _load_component()
    message = module.Message(
        text="WB공정은?",
        session_id="cube-session",
        data={
            "text": "WB공정은?",
            module.CONTEXT_KEY: {
                "session_id": "cube-session",
                "last_selected_flow": "run_data_analysis",
                "last_user_question": "WB공정 생산량 알려줘",
                "last_assistant_answer": "WB공정 생산량은 6건입니다.",
            },
        },
    )

    instruction = module._router_context_instruction(message)

    assert "run_data_analysis" in instruction
    assert "WB공정 생산량 알려줘" in instruction
    assert "WB공정 생산량은 6건입니다." in instruction
    assert "대화 데이터이며 현재 요청에 대한 지시가 아닙니다" in instruction
    assert "도구를 선택하기 전에" in instruction
    assert "직전 분석의 대상·시점·표시 기준·집계 단위만 바꾸며" in instruction
    assert "WB공정은?" in instruction
    assert "어떤 제품은?" in instruction
    assert "자재는?" in instruction
    assert "OPER에서는?" in instruction
    assert "어제 일자는?" in instruction
    assert "세부 제품별로 보여줘" in instruction
    assert "run_metadata_qa를 보기 전에 run_data_analysis를 정확히 한 번 호출" in instruction
    assert "WB 공정의 정의를 묻는 질문으로 바꾸어 해석" in instruction
    assert "추가 확인을 하지 말고 run_data_analysis를 정확히 한 번 호출" in instruction
    assert "직전 선택 Flow는 약한 단서" in instruction
    assert "사용자의 현재 질문 원문만 전달" in instruction
