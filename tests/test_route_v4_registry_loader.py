from __future__ import annotations

import json

from test_langflow_components import ROOT, install_fake_pymongo, load_module


LOADER_PATH = (
    ROOT
    / "langflow_components"
    / "route_flow_v4"
    / "00a_mongodb_workflow_registry_loader.py"
)
PARSER_PATH = ROOT / "langflow_components" / "route_flow_v4" / "00_workflow_plan_parser.py"


def _stored_workflow(key: str, title: str, keywords: list[str]) -> dict:
    return {
        "_id": f"workflow:{key}",
        "section": "workflow_skills",
        "key": key,
        "status": "active",
        "payload": {
            "display_name": title,
            "description": f"{title} 업무를 순서대로 실행합니다.",
            "aliases": [title],
            "intent_examples": [f"오늘 {title} 해줘"],
            "keywords": keywords,
            "excluded_keywords": [],
            "priority": 100,
            "steps": [
                {
                    "step_id": "first",
                    "tool_name": "run_data_analysis",
                    "question": f"{title} 데이터를 조회해줘.",
                    "depends_on": [],
                    "handoff": "none",
                    "on_error": "stop",
                }
            ],
        },
    }


def test_route_v4_registry_loader_reads_stored_schema_and_returns_only_related_candidates(monkeypatch):
    loader = load_module(LOADER_PATH)
    store = install_fake_pymongo(monkeypatch)
    collection = store.setdefault("datagov", {}).setdefault("agent_v4_workflow_skills", {})
    collection["workflow:wb_production"] = _stored_workflow(
        "wb_production",
        "WB 당일 생산량",
        ["WB", "생산량"],
    )
    collection["workflow:lot_hold"] = _stored_workflow(
        "lot_hold",
        "이상 LOT HOLD 이력",
        ["이상 LOT", "HOLD"],
    )

    result = loader.load_workflow_registry_candidates(
        "오늘 WB 공정 생산량을 알려줘",
        "mongodb",
        "mongodb://fake",
        "datagov",
        "agent_v4_workflow_skills",
    )
    registry = json.loads(result["workflow_registry_json"])

    assert result["status"] == "ok"
    assert result["candidate_keys"] == ["wb_production"]
    assert list(registry["workflows"]) == ["wb_production"]
    assert registry["meta"]["source"] == "mongodb"
    assert "mongo_uri" not in result
    assert result["candidate_count"] <= 8


def test_route_v4_registry_loader_never_falls_back_to_inline_seed_in_mongodb_mode():
    loader = load_module(LOADER_PATH)
    inline_seed = {
        "workflows": {
            "must_not_load": {
                "title": "자동 fallback 금지",
                "steps": [
                    {
                        "step_id": "first",
                        "tool_name": "run_data_analysis",
                        "question": "실행하지 마세요.",
                        "depends_on": [],
                        "handoff": "none",
                        "on_error": "stop",
                    }
                ],
            }
        }
    }

    result = loader.load_workflow_registry_candidates(
        "must_not_load",
        "mongodb",
        "",
        "datagov",
        "agent_v4_workflow_skills",
        json.dumps(inline_seed, ensure_ascii=False),
    )
    registry = json.loads(result["workflow_registry_json"])

    assert result["status"] == "error"
    assert result["candidate_keys"] == []
    assert registry["workflows"] == {}
    assert registry["meta"]["source"] == "mongodb"
    assert registry["meta"]["errors"][0]["type"] == "missing_mongo_uri"


def test_route_v4_registry_component_reloads_candidates_when_cached_graph_question_changes():
    loader = load_module(LOADER_PATH)
    seed = {
        "workflows": {
            "wb_production": _stored_workflow("wb_production", "WB 생산량", ["WB", "생산량"])["payload"],
            "lot_hold": _stored_workflow("lot_hold", "이상 LOT HOLD", ["이상 LOT", "HOLD"])["payload"],
        }
    }
    component = loader.MongoDBWorkflowRegistryLoader()
    component.registry_source = "inline_seed"
    component.inline_seed_json = json.dumps(seed, ensure_ascii=False)
    component.mongo_uri = ""
    component.mongo_database = "datagov"
    component.collection_name = "agent_v4_workflow_skills"
    component.status_filter = "active"
    component.max_items = "1000"
    component.candidate_limit = "8"
    component.max_registry_bytes = "65536"

    component.user_question = "오늘 WB 생산량을 알려줘"
    first = json.loads(component.build_registry_message().text)
    component.user_question = "이상 LOT의 HOLD 이력을 알려줘"
    second = json.loads(component.build_registry_message().text)

    assert list(first["workflows"]) == ["wb_production"]
    assert list(second["workflows"]) == ["lot_hold"]


def test_route_v4_parser_uses_canonical_registry_steps_when_planner_returns_registered_key():
    parser = load_module(PARSER_PATH)
    registry = {
        "contract_version": "workflow.registry.v1",
        "workflows": {
            "registered_flow": {
                "workflow_key": "registered_flow",
                "aliases": ["등록 업무"],
                "title": "등록 업무",
                "steps": [
                    {
                        "step_id": "saved_step",
                        "tool_name": "run_data_analysis",
                        "question": "저장된 질문을 실행해줘.",
                        "depends_on": [],
                        "handoff": "none",
                        "on_error": "stop",
                    }
                ],
            }
        },
    }
    rewritten_by_model = {
        "workflow_key": "registered_flow",
        "steps": [
            {
                "step_id": "model_changed_step",
                "tool_name": "run_data_analysis",
                "question": "모델이 바꾼 질문",
                "depends_on": [],
                "handoff": "none",
                "on_error": "stop",
            }
        ],
    }

    result = parser.parse_workflow_plan(
        json.dumps(rewritten_by_model, ensure_ascii=False),
        workflow_registry_json=json.dumps(registry, ensure_ascii=False),
        user_question="자연어 요청",
        allowed_tools_value=["run_data_analysis"],
        workflow_run_id="canonical-plan-run",
    )

    assert result["status"] == "ok"
    assert result["source_kind"] == "registry"
    assert [step["step_id"] for step in result["workflow_plan"]["steps"]] == ["saved_step"]
