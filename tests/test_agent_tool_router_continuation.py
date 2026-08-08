from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = (
    ROOT
    / "langflow_components"
    / "route_flow_v2_continuation"
    / "01_cached_continuation_run_flow_tool.py"
)
EXPORT_PATH = ROOT / "flow_exports" / "09_agent_tool_router_continuation_flow_v5_standalone.json"
CONTINUATION_API_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2_continuation"
    / "22_continuation_api_response_builder.py"
)


def _load_component():
    spec = importlib.util.spec_from_file_location("test_cached_continuation_tool", COMPONENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_file_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pending_payload(session_id: str = "session-1") -> dict:
    contract = {
        "version": "dependent_retrieval.v1",
        "plan_id": "plan-1",
        "plan_hash": "hash-1",
        "session_id": session_id,
        "stages": [
            {"stage_id": "current_lots"},
            {
                "stage_id": "latest_history",
                "input_bindings": [
                    {
                        "source_column": "LOT_ID",
                        "target_param": "LOT_ID",
                        "operator": "in",
                    }
                ],
            },
        ],
    }
    return {
        "response_type": "data_analysis",
        "status": "ok",
        "message": "중간 단계",
        "request": {"session_id": session_id},
        "data": {
            "row_count": 3,
            "data_ref": {"ref_id": "result:session-1:abc", "role": "analysis_result"},
        },
        "continuation": {
            "status": "pending",
            "plan_id": "plan-1",
            "plan_hash": "hash-1",
            "stage_index": 1,
            "max_stages": 2,
            "current_stage_id": "current_lots",
            "next_stage_id": "latest_history",
            "continuation_ref": "continuation:plan-1:hash-1",
            "continuation_contract": contract,
        },
    }


def _final_payload(session_id: str = "session-1") -> dict:
    return {
        "response_type": "data_analysis",
        "status": "ok",
        "message": "최종 HOLD LOT 및 최근 사유 결과입니다.",
        "request": {"session_id": session_id},
        "data": {"row_count": 3, "rows": [{"LOT_ID": "L1"}]},
        "trace": {"generated_code": "do not expose"},
        "continuation": {
            "status": "complete",
            "plan_id": "plan-1",
            "plan_hash": "hash-1",
            "stage_index": 2,
            "current_stage_id": "latest_history",
            "next_stage_id": "",
        },
    }


class _Harness:
    def __init__(self, module, responses):
        self._module = module
        self._attributes = {"flow_tweak_data": {"question": "현재 HOLD LOT와 최근 사유 알려줘"}}
        self.required_all_keywords = ""
        self.required_any_phrases = ""
        self.keyword_gate_message = ""
        self.enable_auto_continuation = True
        self.max_continuation_stages = 2
        self.continuation_timeout_seconds = 60
        self._resolved_continuation_input_ids = {
            name: "request-loader" for name in module.CONTINUATION_INPUT_NAMES
        }
        self._responses = list(responses)
        self.calls = []

    def _inherit_runtime_session(self):
        return "session-1"

    async def _execute_stage(self, args, timeout):
        self.calls.append((dict(args), timeout))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_structured_pending_contract_runs_exactly_two_stages_and_returns_compact_message():
    module = _load_component()
    harness = _Harness(module, [_pending_payload(), _final_payload()])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert message.text == "최종 HOLD LOT 및 최근 사유 결과입니다."
    assert len(harness.calls) == 2
    assert harness.calls[0][0] == {"question": "현재 HOLD LOT와 최근 사유 알려줘"}
    second = harness.calls[1][0]
    assert second["question"] == harness.calls[0][0]["question"]
    assert second["upstream_result_ref"] == "result:session-1:abc"
    assert second["continuation_ref"] == "continuation:plan-1:hash-1"
    assert json.loads(second["continuation_contract"])["plan_hash"] == "hash-1"
    assert second["skip_intermediate_answer"] is False
    assert message.data["continuation_execution"] == {
        "status": "ok",
        "stages_executed": 2,
        "auto_continued": True,
        "final_stage": "latest_history",
    }
    compact_contract = {
        "continuation_execution": {
            "status": "ok",
            "stages_executed": 2,
            "auto_continued": True,
            "final_stage": "latest_history",
        }
    }
    serialized = json.dumps(compact_contract, ensure_ascii=False)
    assert "rows" not in serialized
    assert "generated_code" not in serialized
    assert "trace" not in serialized


@pytest.mark.asyncio
async def test_router_consumes_real_continuation_api_projection_without_answer_parsing():
    module = _load_component()
    api = _load_file_module(CONTINUATION_API_PATH, "test_continuation_api_projection")
    dependent_plan = {
        "version": "analysis.dependent_retrieval.v1",
        "plan_id": "plan-1",
        "plan_hash": "hash-1",
        "max_stages": 2,
        "stages": [
            {
                "stage_id": "current_lots",
                "retrieval_jobs": [{"dataset_key": "lot_status", "source_alias": "lot_status"}],
                "pandas_execution_plan": [],
                "output_contract": {"result_columns": ["LOT_ID"]},
                "handoff": {"columns": ["LOT_ID"]},
            },
            {
                "stage_id": "latest_history",
                "depends_on": ["current_lots"],
                "input_bindings": [
                    {
                        "source_stage_id": "current_lots",
                        "source_column": "LOT_ID",
                        "target_param": "LOT_ID",
                        "operator": "in",
                    }
                ],
                "retrieval_jobs": [{"dataset_key": "hold_history", "source_alias": "hold_history"}],
                "pandas_execution_plan": [],
                "output_contract": {"result_columns": ["LOT_ID", "HOLD_DESC"]},
            },
        ],
        "runtime": {
            "status": "pending",
            "active_stage_index": 0,
            "current_stage_id": "current_lots",
            "next_stage_id": "latest_history",
            "intent_llm_skipped": False,
        },
    }
    projected = api.build_api_response(
        {
            "request": {"question": "현재 HOLD LOT와 최근 사유 알려줘", "session_id": "session-1"},
            "intent_plan": {"dependent_retrieval_plan": dependent_plan},
            "analysis": {"status": "ok"},
            "data": {
                "row_count": 3,
                "data_ref": {"ref_id": "result:session-1:abc", "role": "analysis_result"},
            },
            "trace": {"inspection": {"result_store": {"status": "ok"}}},
            "answer_message": "다음 조회 단계를 준비했습니다.",
        }
    )
    assert projected["continuation"]["status"] == "pending"
    harness = _Harness(module, [projected, _final_payload()])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.text == "최종 HOLD LOT 및 최근 사유 결과입니다."
    assert harness.calls[1][0]["continuation_ref"] == "continuation:plan-1:hash-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda value: value["data"].update({"row_count": 0}), "empty_stage_result"),
        (lambda value: value["data"].update({"data_ref": {}}), "result_ref_missing"),
        (
            lambda value: value["data"].update(
                {"data_ref": {"ref_id": "r" * 1025}}
            ),
            "result_ref_invalid",
        ),
        (lambda value: value["continuation"].update({"continuation_contract": {}}), "continuation_contract_missing"),
        (
            lambda value: value["continuation"].update(
                {"continuation_contract": {"plan_id": "plan-1", "plan_hash": "hash-1"}}
            ),
            "continuation_binding_missing",
        ),
        (lambda value: value["continuation"].update({"continuation_ref": ""}), "continuation_ref_missing"),
        (lambda value: value["request"].pop("session_id"), "session_missing"),
        (lambda value: value["request"].update({"session_id": "other-session"}), "session_mismatch"),
        (
            lambda value: value["continuation"]["continuation_contract"].update(
                {"plan_hash": "tampered-hash"}
            ),
            "continuation_contract_mismatch",
        ),
        (
            lambda value: value["continuation"]["continuation_contract"].update(
                {"session_id": "other-session"}
            ),
            "continuation_contract_mismatch",
        ),
        (
            lambda value: value["continuation"]["continuation_contract"].pop("session_id"),
            "continuation_contract_mismatch",
        ),
    ],
)
async def test_incomplete_pending_contract_fails_closed_without_second_call(mutator, reason):
    module = _load_component()
    first = _pending_payload()
    mutator(first)
    harness = _Harness(module, [first])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == reason


@pytest.mark.asyncio
async def test_single_stage_response_keeps_one_child_call():
    module = _load_component()
    single = _final_payload()
    single["continuation"] = {"status": "complete", "current_stage_id": "single"}
    harness = _Harness(module, [single])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.text == single["message"]
    assert message.data["continuation_execution"]["auto_continued"] is False
    assert message.data["continuation_execution"]["stages_executed"] == 1


@pytest.mark.asyncio
async def test_not_applicable_single_stage_contract_keeps_one_child_call():
    module = _load_component()
    single = _final_payload()
    single["continuation"] = {"status": "not_applicable"}
    harness = _Harness(module, [single])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.text == single["message"]
    assert message.data["continuation_execution"]["status"] == "ok"
    assert message.data["continuation_execution"]["auto_continued"] is False


@pytest.mark.asyncio
async def test_blocked_single_stage_contract_is_not_presented_as_success():
    module = _load_component()
    single = _final_payload()
    single["continuation"] = {"status": "blocked", "current_stage_id": "single"}
    harness = _Harness(module, [single])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == "continuation_result_error"


@pytest.mark.asyncio
async def test_second_pending_response_is_not_returned_as_success():
    module = _load_component()
    harness = _Harness(module, [_pending_payload(), _pending_payload()])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == "continuation_still_pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["continuation"].update({"status": "blocked"}),
        lambda value: value["continuation"].update({"current_stage_id": "wrong_stage"}),
        lambda value: value["continuation"].update({"stage_index": 1}),
        lambda value: value["continuation"].update({"next_stage_id": "unexpected_stage"}),
        lambda value: value["continuation"].pop("plan_id"),
        lambda value: value["continuation"].pop("plan_hash"),
    ],
)
async def test_invalid_terminal_continuation_contract_fails_closed(mutator):
    module = _load_component()
    final = _final_payload()
    mutator(final)
    harness = _Harness(module, [_pending_payload(), final])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == "continuation_result_error"


@pytest.mark.asyncio
async def test_terminal_missing_session_evidence_fails_closed():
    module = _load_component()
    final = _final_payload()
    final["request"].pop("session_id")
    harness = _Harness(module, [_pending_payload(), final])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == "session_missing"


@pytest.mark.asyncio
async def test_empty_complete_terminal_continuation_is_a_valid_final_state():
    module = _load_component()
    final = _final_payload()
    final["message"] = "조건에 맞는 최신 이력이 없습니다."
    final["data"]["row_count"] = 0
    final["data"]["rows"] = []
    final["continuation"]["status"] = "empty_complete"
    harness = _Harness(module, [_pending_payload(), final])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.text == "조건에 맞는 최신 이력이 없습니다."
    assert message.data["continuation_execution"]["status"] == "ok"
    assert message.data["continuation_execution"]["final_stage"] == "latest_history"


@pytest.mark.asyncio
async def test_missing_child_input_port_blocks_before_second_call():
    module = _load_component()
    harness = _Harness(module, [_pending_payload()])
    harness._resolved_continuation_input_ids["continuation_contract"] = ""

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.data["continuation_execution"]["failure_reason"] == "continuation_port_missing"


@pytest.mark.asyncio
async def test_partial_second_stage_is_not_presented_as_complete():
    module = _load_component()
    final = _final_payload()
    final["status"] = "partial"
    harness = _Harness(module, [_pending_payload(), final])

    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 2
    assert message.data["continuation_execution"]["status"] == "blocked"
    assert message.data["continuation_execution"]["failure_reason"] == "continuation_result_error"


@pytest.mark.asyncio
async def test_child_timeout_fails_closed_without_retrying():
    module = _load_component()
    harness = _Harness(module, [])

    async def raise_timeout(args, timeout):
        harness.calls.append((dict(args), timeout))
        raise TimeoutError

    harness._execute_stage = raise_timeout
    message = await module.CachedContinuationRunFlowTool._run_selected_flow(harness)

    assert len(harness.calls) == 1
    assert message.data["continuation_execution"]["failure_reason"] == "child_timeout"


def test_export_is_additive_and_keeps_agent_single_iteration_direct_return_contract():
    flow = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))

    assert flow["name"] == "09. v5_agent_tool_router_continuation"
    assert flow["endpoint_name"] == "metadata-driven-v5-agent-tool-router-continuation"
    assert flow["last_tested_version"] == "1.9.2"
    nodes = flow["data"]["nodes"]
    agent = next(node for node in nodes if node["id"] == "Agent-agent-tool-router-continuation")
    assert agent["data"]["node"]["template"]["max_iterations"]["value"] == 1
    continuation_tool = next(node for node in nodes if node["id"] == "ContinuationFlowTool-data_analysis")
    template = continuation_tool["data"]["node"]["template"]
    assert continuation_tool["data"]["type"] == "CachedContinuationRunFlowTool"
    assert template["flow_name_selected"]["value"] == "08. v5_data_analysis_continuation"
    assert template["preferred_output_names"]["value"] == "api_response"
    assert template["enable_auto_continuation"]["value"] is True
    assert template["max_continuation_stages"]["value"] == 2
    assert template["return_direct"]["value"] is True
    assert all(node["data"]["node"]["lf_version"] == "1.9.2" for node in nodes)
