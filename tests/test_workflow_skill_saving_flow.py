from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from string import Formatter


ROOT = Path(__file__).resolve().parents[1]
FLOW_ROOT = ROOT / "langflow_components" / "workflow_skill_saving_flow"
REGISTRY_PATH = ROOT / "docs" / "workflows" / "workflow_registry.example.json"
PROMPT_PATH = FLOW_ROOT / "03_saving_prompt_template_ko.md"


def _load(filename: str):
    path = FLOW_ROOT / filename
    spec = importlib.util.spec_from_file_location(f"workflow_skill_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"모듈을 불러올 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_llm_result() -> dict:
    return {
        "items": [
            {
                "section": "workflow_skills",
                "key": "equipment_uph_source_audit",
                "status": "active",
                "payload": {
                    "display_name": "장비 UPH와 메타데이터 조회",
                    "description": "장비와 UPH를 조회한 뒤 관련 데이터셋 정의를 확인합니다.",
                    "aliases": ["장비 UPH 감사"],
                    "intent_examples": ["D/A1 장비 UPH와 데이터 소스를 알려줘"],
                    "keywords": ["장비", "UPH", "데이터 소스"],
                    "excluded_keywords": [],
                    "priority": 100,
                    "steps": [
                        {
                            "step_id": "production",
                            "tool_name": "run_data_analysis",
                            "question": "현재 D/A1 장비와 UPH를 조회해.",
                            "depends_on": [],
                            "handoff": "none",
                            "on_error": "stop",
                        },
                        {
                            "step_id": "metadata",
                            "tool_name": "run_metadata_qa",
                            "question": "equipment_assign과 eqp_uph 정의를 알려줘.",
                            "depends_on": ["production"],
                            "handoff": "none",
                            "on_error": "continue",
                        },
                    ],
                },
            }
        ],
        "refinement": {
            "needs_more_input": False,
            "missing_information": [],
            "assumptions": [],
        },
    }


def _normalized_payload(action: str = "replace") -> dict:
    request = _load("00_workflow_skill_saving_request_loader.py")
    normalizer = _load("04_workflow_skill_saving_result_normalizer.py")
    payload = request.build_request("장비 UPH Workflow Skill을 등록해줘.", action, True)
    return normalizer.normalize_authoring(payload, json.dumps(_valid_llm_result(), ensure_ascii=False))


def test_workflow_skill_request_defaults_to_safe_dry_run():
    request = _load("00_workflow_skill_saving_request_loader.py")

    payload = request.build_request("Workflow Skill 등록", "ask", None)

    assert payload["request"]["dry_run"] is True
    assert payload["request"]["duplicate_action"] == "skip"
    assert payload["metadata_type"] == "workflow_skill"


def test_workflow_skill_normalizer_accepts_valid_two_step_sequence():
    payload = _normalized_payload()

    assert payload["errors"] == []
    assert payload["items"][0]["key"] == "equipment_uph_source_audit"
    assert [step["tool_name"] for step in payload["items"][0]["payload"]["steps"]] == [
        "run_data_analysis",
        "run_metadata_qa",
    ]


def test_workflow_skill_normalizer_accepts_data_analysis_to_html_visualization_handoff():
    request = _load("00_workflow_skill_saving_request_loader.py")
    normalizer = _load("04_workflow_skill_saving_result_normalizer.py")
    writer = _load("07_workflow_skill_review_writer.py")
    llm_result = _valid_llm_result()
    steps = llm_result["items"][0]["payload"]["steps"]
    steps[1] = {
        "step_id": "chart",
        "tool_name": "run_visualization",
        "question": "일자를 X축, 생산량을 Y축으로 사용한 선 그래프 HTML을 만들어줘.",
        "depends_on": ["production"],
        "handoff": "result_ref",
        "on_error": "stop",
    }

    payload = normalizer.normalize_authoring(
        request.build_request("최근 생산량 HTML 차트 Workflow", "create_new", True),
        llm_result,
    )

    assert payload["errors"] == []
    assert payload["items"][0]["payload"]["steps"][1]["tool_name"] == "run_visualization"
    assert payload["items"][0]["payload"]["steps"][1]["handoff"] == "result_ref"
    assert writer._validate_item(payload["items"][0]) == []


def test_workflow_skill_normalizer_blocks_invalid_result_ref_source():
    request = _load("00_workflow_skill_saving_request_loader.py")
    normalizer = _load("04_workflow_skill_saving_result_normalizer.py")
    llm_result = _valid_llm_result()
    steps = llm_result["items"][0]["payload"]["steps"]
    steps[0]["tool_name"] = "run_metadata_qa"
    steps[1]["tool_name"] = "run_data_analysis"
    steps[1]["handoff"] = "result_ref"

    payload = normalizer.normalize_authoring(
        request.build_request("잘못된 result ref Workflow", "create_new", True),
        llm_result,
    )

    assert any(error["type"] == "result_ref_source_not_supported" for error in payload["errors"])


def test_workflow_skill_normalizer_does_not_silently_truncate_invalid_execution_fields():
    request = _load("00_workflow_skill_saving_request_loader.py")
    normalizer = _load("04_workflow_skill_saving_result_normalizer.py")
    llm_result = _valid_llm_result()
    llm_result["items"][0]["key"] = "1_invalid_key"
    llm_result["items"][0]["payload"]["steps"][0]["step_id"] = "잘못된 단계"
    llm_result["items"][0]["payload"]["steps"][0]["question"] = "가" * 4001

    payload = normalizer.normalize_authoring(
        request.build_request("무효 Workflow", "create_new", True),
        llm_result,
    )

    error_types = {error["type"] for error in payload["errors"]}
    assert {"invalid_workflow_key", "invalid_step_id", "step_question_too_long"} <= error_types
    assert payload["items"][0]["key"] == "1_invalid_key"
    assert len(payload["items"][0]["payload"]["steps"][0]["question"]) == 4001


def test_workflow_skill_normalizer_blocks_oversize_payload():
    request = _load("00_workflow_skill_saving_request_loader.py")
    normalizer = _load("04_workflow_skill_saving_result_normalizer.py")
    llm_result = _valid_llm_result()
    llm_result["items"][0]["payload"]["description"] = "긴 설명" * 9000

    payload = normalizer.normalize_authoring(
        request.build_request("과대 Workflow", "create_new", True),
        llm_result,
    )

    assert any(error["type"] == "workflow_payload_too_large" for error in payload["errors"])


def test_workflow_skill_replace_dry_run_targets_unique_canonical_document():
    matcher = _load("05_workflow_skill_similarity_checker.py")
    writer = _load("07_workflow_skill_review_writer.py")
    payload = _normalized_payload("replace")
    existing = {
        "_id": "workflow:equipment_uph",
        "section": "workflow_skills",
        "key": "equipment_uph",
        "status": "active",
        "payload": {
            "display_name": "기존 장비 UPH Workflow",
            "description": "기존 정의",
            "aliases": ["장비 UPH 감사"],
            "steps": _valid_llm_result()["items"][0]["payload"]["steps"],
        },
    }

    matched = matcher.check_similarity(payload, {"existing_items": [existing]})
    result = writer.review_and_write(matched)

    assert result["write_result"]["success"] is True
    assert result["write_result"]["operation_by_key"][0]["operation"] == "replaced"
    assert result["write_result"]["operation_by_key"][0]["target_key"] == "equipment_uph"


def test_workflow_skill_replace_dry_run_inserts_when_no_similar_document_exists():
    writer = _load("07_workflow_skill_review_writer.py")
    payload = _normalized_payload("replace")
    payload["trace"]["duplicate_lookup"] = {
        "status": "ok",
        "combined_count": 0,
        "errors": [],
    }

    result = writer.review_and_write(payload)

    assert result["write_result"]["success"] is True
    assert result["write_result"]["operation_by_key"][0]["operation"] == "inserted"


def test_workflow_skill_ambiguous_replace_is_blocked():
    matcher = _load("05_workflow_skill_similarity_checker.py")
    writer = _load("07_workflow_skill_review_writer.py")
    payload = _normalized_payload("replace")
    base = _valid_llm_result()["items"][0]["payload"]
    existing_items = [
        {
            "_id": f"workflow:existing_{index}",
            "section": "workflow_skills",
            "key": f"existing_{index}",
            "status": "active",
            "payload": {**base, "aliases": ["장비 UPH 감사"]},
        }
        for index in (1, 2)
    ]

    matched = matcher.check_similarity(payload, {"existing_items": existing_items})
    result = writer.review_and_write(matched)

    assert result["write_result"]["success"] is False
    assert any(error["type"] == "ambiguous_replace_target" for error in result["write_result"]["errors"])


def test_workflow_skill_message_renders_steps_without_raw_json_table_cells():
    response = _load("08_workflow_skill_saving_response_builder.py")
    message = _load("09_workflow_skill_saving_message_adapter.py")
    writer = _load("07_workflow_skill_review_writer.py")
    payload = _normalized_payload("replace")
    payload["trace"]["duplicate_lookup"] = {"status": "ok", "combined_count": 0, "errors": []}

    text = message.build_message(response.build_response(writer.review_and_write(payload)))

    assert "### 실행 순서" in text
    assert "run_data_analysis" in text
    assert '"steps"' not in text


def test_workflow_skill_input_examples_cover_supported_tools_and_execution_checks():
    text = (FLOW_ROOT / "INPUT_EXAMPLES.md").read_text(encoding="utf-8")

    for workflow_key in (
        "daily_manufacturing_briefing",
        "hold_lot_history_metadata_audit",
        "equipment_uph_source_audit",
        "recent_da_production_chart",
    ):
        assert workflow_key in text

    for tool_name in ("run_data_analysis", "run_metadata_qa", "run_visualization"):
        assert tool_name in text

    assert "08 실행 확인 질문" in text
    assert "handoff=result_ref" in text
    assert "anomaly_lot_hold_history" not in text
    assert "save_domain_metadata" not in text
    assert "save_table_catalog_metadata" not in text
    assert "save_main_flow_filter_metadata" not in text


def test_recommended_workflow_registry_contains_only_three_executable_read_flows():
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    workflows = registry["workflows"]

    assert registry["contract_version"] == "workflow.registry.v1"
    assert {workflow["workflow_key"] for workflow in workflows} == {
        "daily_manufacturing_briefing",
        "hold_lot_history_metadata_audit",
        "equipment_uph_source_audit",
    }

    supported_tools = {"run_data_analysis", "run_metadata_qa"}
    for workflow in workflows:
        assert 1 <= len(workflow["steps"]) <= 4
        assert {step["tool_name"] for step in workflow["steps"]} <= supported_tools
        assert any(step["tool_name"] == "run_data_analysis" for step in workflow["steps"])
        assert any(step["tool_name"] == "run_metadata_qa" for step in workflow["steps"])

    hold_workflow = next(
        workflow for workflow in workflows if workflow["workflow_key"] == "hold_lot_history_metadata_audit"
    )
    hold_history = next(step for step in hold_workflow["steps"] if step["step_id"] == "hold_history")
    assert hold_history["tool_name"] == "run_data_analysis"
    assert hold_history["depends_on"] == ["current_hold_lots"]
    assert hold_history["handoff"] == "result_ref"


def test_workflow_skill_prompt_has_one_valid_f_string_variable():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    fields = [field for _, field, _, _ in Formatter().parse(prompt) if field]

    assert fields == ["source_text"]
    rendered = prompt.format(source_text="테스트 Workflow를 등록해줘.")
    assert '"items"' in rendered
    assert '"refinement"' in rendered
    assert "테스트 Workflow를 등록해줘." in rendered
    assert "run_visualization" in rendered
