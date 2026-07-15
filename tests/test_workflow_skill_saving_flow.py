from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_ROOT = ROOT / "langflow_components" / "workflow_skill_saving_flow"


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
                "key": "wb_daily_production_metadata",
                "status": "active",
                "payload": {
                    "display_name": "WB 생산량과 메타데이터 조회",
                    "description": "WB 생산량을 조회한 뒤 관련 공정 그룹 정의를 조회합니다.",
                    "aliases": ["WB 일일 브리핑"],
                    "intent_examples": ["오늘 WB 생산량과 공정 정의를 알려줘"],
                    "keywords": ["WB", "생산량", "공정 정의"],
                    "excluded_keywords": [],
                    "priority": 100,
                    "steps": [
                        {
                            "step_id": "production",
                            "tool_name": "run_data_analysis",
                            "question": "오늘 WB 공정 생산량을 조회해.",
                            "depends_on": [],
                            "handoff": "none",
                            "on_error": "stop",
                        },
                        {
                            "step_id": "metadata",
                            "tool_name": "run_metadata_qa",
                            "question": "등록된 WB 공정 그룹 정의를 알려줘.",
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
    payload = request.build_request("WB Workflow Skill을 등록해줘.", action, True)
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
    assert payload["items"][0]["key"] == "wb_daily_production_metadata"
    assert [step["tool_name"] for step in payload["items"][0]["payload"]["steps"]] == [
        "run_data_analysis",
        "run_metadata_qa",
    ]


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
        "_id": "workflow:wb_daily",
        "section": "workflow_skills",
        "key": "wb_daily",
        "status": "active",
        "payload": {
            "display_name": "기존 WB Workflow",
            "description": "기존 정의",
            "aliases": ["WB 일일 브리핑"],
            "steps": _valid_llm_result()["items"][0]["payload"]["steps"],
        },
    }

    matched = matcher.check_similarity(payload, {"existing_items": [existing]})
    result = writer.review_and_write(matched)

    assert result["write_result"]["success"] is True
    assert result["write_result"]["operation_by_key"][0]["operation"] == "replaced"
    assert result["write_result"]["operation_by_key"][0]["target_key"] == "wb_daily"


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
            "payload": {**base, "aliases": ["WB 일일 브리핑"]},
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
