from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from tools import validate_data_analysis_v2_continuation as validator
from tools import validate_representative_questions as base
from tools import build_continuation_import_ready_bundle as continuation_bundle
from tools import validate_flow_component_sources as source_validator
from tools.build_continuation_import_ready_bundle import build_continuation_bundle
from tools.build_import_ready_bundle import build_bundle as build_base_bundle


ROOT = Path(__file__).resolve().parents[1]


def test_continuation_manifest_covers_canonical_30_and_safety_cases():
    manifest = validator.load_manifest()
    regression = manifest["regression_cases"]
    continuation = {item["id"]: item for item in manifest["continuation_cases"]}

    assert [item["base_case_id"] for item in regression] == list(range(1, 31))
    assert regression[27]["id"] == "R28"
    assert regression[27]["parity_exception"] == "metadata_defined_latest_hold_history"
    assert regression[29]["id"] == "R30"
    assert regression[29]["expected_route"] == "complex"
    assert continuation["C02"]["expected_child_calls"] == 1
    assert continuation["C02"]["scenario"] == "independent_multi_source"
    assert continuation["C12"]["filters"]["RECIPE_ID"] == {
        "operator": "starts_with",
        "value": "R0429",
    }
    assert continuation["C13"]["datasets"] == ["lot_status"]
    assert continuation["C13"]["forbidden_datasets"] == ["hold_history"]


def test_live_suite_covers_all_regressions_and_required_continuation_cases():
    specs = validator._live_case_specs(validator.load_manifest())

    assert [item["id"] for item in specs[:30]] == [f"R{index:02d}" for index in range(1, 31)]
    assert {item["id"] for item in specs[30:]} == validator.LIVE_REQUIRED_CONTINUATION_IDS


def test_source_validator_discovers_both_exact_continuation_export_names():
    keys = {
        str(item.get("endpoint_name") or item.get("name") or "")
        for item in source_validator._flow_exports()
    }

    assert "metadata-driven-v5-data-analysis-continuation" in keys
    assert "metadata-driven-v5-agent-tool-router-continuation" in keys


def test_live_model_recorder_counts_actual_calls_and_prompt_sizes(monkeypatch):
    checkpoint = validator.LiveCheckpoint(None, model=validator.DEFAULT_LIVE_MODEL, reference_date="20260701")
    recorder = validator.LiveModelRecorder(
        "R01",
        {"model": validator.DEFAULT_LIVE_MODEL},
        checkpoint,
    )
    monkeypatch.setattr(base, "call_llm", lambda prompt, config: '{"ok":true}')

    response = recorder.invoker("intent")("한글 prompt")
    summary = recorder.summary()

    assert response == '{"ok":true}'
    assert summary["actual_calls"] == {"intent": 1, "pandas": 0, "answer": 0}
    assert summary["prompt_chars"]["intent"] == len("한글 prompt")
    assert summary["prompt_bytes"]["intent"] == len("한글 prompt".encode("utf-8"))


def test_live_default_model_is_gemini_35_flash_lite(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.setattr(base, "resolve_llm_config", lambda: {"model": "old", "api_key": "x"})

    assert validator._resolve_live_llm_config()["model"] == "gemini-3.5-flash-lite"


def test_live_route_mismatch_is_advisory_except_for_continuation_control_flow():
    assert validator._live_route_mismatch_is_error("fast", "complex") is False
    assert validator._live_route_mismatch_is_error("complex", "fast") is False
    assert validator._live_route_mismatch_is_error("continuation", "fast") is True
    assert validator._live_route_mismatch_is_error("fast", "continuation") is True


@pytest.mark.parametrize("scenario_id", ["M01", "M02"])
def test_multiturn_scenarios_execute_result_ref_handoffs(scenario_id: str):
    manifest = validator.load_manifest()
    scenario = next(item for item in manifest["multiturn_scenarios"] if item["id"] == scenario_id)

    result = validator.validate_multiturn_scenario(scenario, manifest["limits"])

    assert result["status"] == "ok", result["errors"]
    assert result["turns"][1]["upstream_result_ref"] == result["turns"][0]["result_ref"]


def test_question_document_preserves_the_exact_canonical_30_questions():
    text = (ROOT / "validation_questions_v2_continuation.txt").read_text(encoding="utf-8")
    canonical = validator._canonical_v2_questions()
    for case_id, question in canonical.items():
        assert f"R{case_id:02d}. {question}" in text


def test_live_specs_use_canonical_question_file_not_drifted_fixture_wording():
    specs = validator._live_case_specs(validator.load_manifest())
    canonical = validator._canonical_v2_questions()

    assert {int(item["id"][1:]): item["question"] for item in specs[:30]} == canonical

    r04 = specs[3]["base_case"]
    assert r04["intent_response"]["intent_plan"]["retrieval_jobs"][0]["filters"] == {}
    r09 = specs[8]["base_case"]
    assert r09["min_rows"] == 1
    assert "expected_first_row" not in r09
    assert r09["intent_response"]["intent_plan"]["pandas_function_cases"][0]["input_text"] == (
        "SP 24G GDDR7 X32 226 FCBGA DDP"
    )
    r22 = specs[21]["base_case"]
    assert r22["intent_response"]["intent_plan"]["retrieval_jobs"][0]["filters"] == {}


def test_r09_live_fixture_adapter_adds_one_canonical_row_and_decoys_only_to_validation():
    retrieved = {
        "source_results": [
            {
                "dataset_key": "production",
                "applied_params": {"DATE": "20260807"},
                "rows": [{"DEVICE": "EXISTING", "PRODUCTION": 1}],
                "columns": ["DEVICE", "PRODUCTION"],
                "row_count": 1,
            }
        ]
    }

    adapted = validator._augment_live_fixture_retrieval(
        {"id": "R09"},
        retrieved,
        "20260808",
    )
    rows = adapted["source_results"][0]["rows"]

    assert retrieved["source_results"][0]["row_count"] == 1
    assert len(rows) == 4
    assert sum(row.get("DEVICE") == "DEV-SP24-GDDR7-X32-226" for row in rows) == 1
    assert sum(str(row.get("DEVICE") or "").startswith("DECOY-SP24-") for row in rows) == 2


@pytest.mark.parametrize("case_id", ["C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14"])
def test_continuation_fixture_contracts(case_id: str):
    manifest = validator.load_manifest()
    cases = {item["id"]: item for item in manifest["continuation_cases"]}

    result = validator.validate_continuation_case(cases[case_id], manifest["limits"])

    assert result["status"] == "ok", result["errors"]
    assert result["child_calls"] == cases[case_id]["expected_child_calls"]
    assert result["contract_bytes"] <= manifest["limits"]["max_continuation_contract_bytes"]
    assert result["observation_bytes"] <= manifest["limits"]["max_agent_observation_bytes"]
    assert result["stage1_prompt_bytes"] <= manifest["limits"]["max_stage1_prompt_bytes"]
    assert result["stage2_prompt_bytes"] <= manifest["limits"]["max_stage2_prompt_bytes"]


def test_latest_history_fields_come_from_the_same_winning_row_and_left_population_is_preserved():
    manifest = validator.load_manifest()
    case = next(item for item in manifest["continuation_cases"] if item["id"] == "C01")

    result = validator.validate_continuation_case(case, manifest["limits"])
    by_lot = {row["LOT_ID"]: row for row in result["rows"]}

    assert result["status"] == "ok", result["errors"]
    assert by_lot["LOT-A"] == {
        "LOT_ID": "LOT-A",
        "OPER_NAME": "W/B1",
        "HOLD_STAT": "OnHold",
        "HOLD_TM": "2026-08-08 11:00:00",
        "HOLD_CD": "H02",
        "HOLD_DESC": "최신 사유",
    }
    assert by_lot["LOT-C"]["HOLD_TM"] == ""
    assert by_lot["LOT-C"]["HOLD_CD"] == ""
    assert by_lot["LOT-C"]["HOLD_DESC"] == ""


def test_two_stage_contract_skips_second_intent_and_first_answer_models():
    manifest = validator.load_manifest()
    cases = {item["id"]: item for item in manifest["continuation_cases"]}
    for case_id in ("C01", "C09"):
        result = validator.validate_continuation_case(cases[case_id], manifest["limits"])
        assert result["child_calls"] == 2
        assert result["intent_llm_skipped"] is True
        assert result["first_answer_model_calls"] == 0
        assert result["stage2_prompt_bytes"] == 0


def test_blocked_second_stage_never_exposes_first_stage_rows_as_final_success():
    manifest = validator.load_manifest()
    case = next(item for item in manifest["continuation_cases"] if item["id"] == "C09")

    result = validator.validate_continuation_case(case, manifest["limits"])

    assert result["status"] == "ok", result["errors"]
    assert result["execution_status"] == "blocked"
    assert result["failure_reason"] == "stage2_retrieval_failed"
    assert result["rows"] == []


def test_non_lot_upstream_binding_uses_the_same_generic_contract_shape():
    manifest = validator.load_manifest()
    case = {
        "id": "SYNTHETIC",
        "question": "선행 이벤트 결과로 상세 상태를 보강해줘",
        "binding": {
            "source_column": "EVENT_ID",
            "target_param": "EVENT_ID",
            "operator": "in",
        },
        "transforms": [],
    }
    rows = [{"EVENT_ID": "EV-1"}, {"EVENT_ID": "EV-2"}]

    contract = validator._build_continuation_contract(case, rows, manifest["limits"])

    assert contract["binding"] == {
        "source_column": "EVENT_ID",
        "target_param": "EVENT_ID",
        "operator": "in",
        "values": ["EV-1", "EV-2"],
    }
    assert contract["max_stages"] == 2
    assert contract["intent_envelope"]["intent_plan"]["dependent_retrieval_plan"]["active_stage_index"] == 1


def test_production_compiler_lifts_a_non_lot_catalog_dependency_without_question_rules():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    compiler = validator._continuation_modules()["compiler"]
    payload = {"request": {"question": "선행 이벤트 결과로 상세 상태를 보강해줘"}}
    intent = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "event_index", "source_alias": "events", "required_params": {}},
                {"dataset_key": "event_detail", "source_alias": "details", "required_params": {}},
            ],
            "pandas_execution_plan": [
                {
                    "operation": "join",
                    "left_source_alias": "events",
                    "right_source_alias": "details",
                    "join_type": "left",
                }
            ],
            "output_contract": {
                "result_columns": ["EVENT_ID", "STATUS"],
                "required_columns": ["EVENT_ID", "STATUS"],
                "grain_columns": ["EVENT_ID"],
                "metric_columns": [],
            },
        }
    }
    catalog = {
        "table_catalog_items": [
            {
                "key": "event_index",
                "payload": {"canonical_columns": ["EVENT_ID"]},
            },
            {
                "key": "event_detail",
                "payload": {
                    "required_params": ["EVENT_ID"],
                    "canonical_columns": ["EVENT_ID", "STATUS"],
                    "upstream_bindings": [
                        {
                            "source_column": "EVENT_ID",
                            "target_param": "EVENT_ID",
                            "operator": "in",
                            "source_alias": "previous_result",
                        }
                    ],
                },
            },
        ]
    }

    response, trace = compiler.compile_intent_response(
        payload,
        json.dumps(intent, ensure_ascii=False),
        catalog,
    )
    plan = json.loads(response)["intent_plan"]["dependent_retrieval_plan"]

    assert trace["status"] == "pending"
    assert trace["active_stage_index"] == 0
    assert plan["activation"] == {
        "reason": "catalog_required_param_dependency",
        "source": "table_catalog.upstream_bindings",
    }
    assert plan["stages"][1]["input_bindings"][0]["source_column"] == "EVENT_ID"
    assert plan["stages"][1]["input_bindings"][0]["target_param"] == "EVENT_ID"


def test_production_compiler_keeps_independent_multi_source_join_single_stage():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    compiler = validator._continuation_modules()["compiler"]
    intent = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production", "required_params": {"DATE": "20260808"}},
                {"dataset_key": "equipment_assign", "source_alias": "equipment", "required_params": {}},
            ],
            "pandas_execution_plan": [{"operation": "join", "left_source_alias": "production", "right_source_alias": "equipment"}],
            "output_contract": {"result_columns": ["PRODUCT_KEY", "EQP_ID"]},
        }
    }
    catalog = {
        "table_catalog_items": [
            {"key": "production", "payload": {"required_params": ["DATE"], "canonical_columns": ["PRODUCT_KEY"]}},
            {"key": "equipment_assign", "payload": {"required_params": [], "canonical_columns": ["PRODUCT_KEY", "EQP_ID"]}},
        ]
    }

    response, trace = compiler.compile_intent_response(
        {"request": {"question": "상위 제품과 장비를 보여줘"}},
        json.dumps(intent, ensure_ascii=False),
        catalog,
    )

    assert trace == {"status": "passthrough", "dependent": False, "active_stage_index": 0}
    assert "dependent_retrieval_plan" not in json.loads(response)["intent_plan"]


def test_production_extreme_row_primitive_keeps_code_and_description_from_same_row():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    executor = validator._continuation_modules()["executor"]
    history = pd.DataFrame(
        [
            {"LOT_ID": "LOT-A", "HOLD_TM": "2026-08-08 08:00:00", "HOLD_CD": "H01", "HOLD_DESC": "이전"},
            {"LOT_ID": "LOT-A", "HOLD_TM": "2026-08-08 11:00:00", "HOLD_CD": "H02", "HOLD_DESC": "최신"},
        ]
    )
    upstream = pd.DataFrame([{"LOT_ID": "LOT-A", "OPER_NAME": "W/B1"}, {"LOT_ID": "LOT-B", "OPER_NAME": "W/B2"}])
    contract = {
        "operation": "select_extreme_row_per_group",
        "source_alias": "hold_history",
        "partition_by": ["LOT_ID"],
        "order_by": [{"column": "HOLD_TM", "direction": "desc"}],
        "tie_breakers": [],
        "limit_per_group": 1,
        "tie_policy": "first",
        "projection": ["LOT_ID", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
        "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
        "join": {
            "left_source_alias": "upstream_result",
            "left_on": ["LOT_ID"],
            "right_on": ["LOT_ID"],
        },
    }

    result, certificate = executor._execute_select_extreme_row_per_group(
        contract,
        {"hold_history": history, "upstream_result": upstream},
        pd,
    )

    by_lot = {row["LOT_ID"]: row for row in result.to_dict(orient="records")}
    assert by_lot["LOT-A"]["HOLD_TM"] == "2026-08-08 11:00:00"
    assert by_lot["LOT-A"]["HOLD_CD"] == "H02"
    assert by_lot["LOT-A"]["HOLD_DESC"] == "최신"
    assert pd.isna(by_lot["LOT-B"]["HOLD_TM"])
    assert certificate["same_row_projection"] is True
    assert certificate["left_population_preserved"] is True


def _obsolete_test_continuation_request_router_uses_public_intent_envelope():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    modules = validator._continuation_modules()
    manifest = validator.load_manifest()
    case = next(item for item in manifest["continuation_cases"] if item["id"] == "C01")
    contract = validator._build_continuation_contract(case, case["stage1_rows"], manifest["limits"])
    payload = modules["request"].build_request(
        "원래 질문",
        session_id="session-hold",
        upstream_result_ref="result:session-hold:stage1",
        continuation_ref=contract["continuation_ref"],
        continuation_contract=contract,
        skip_intermediate_answer=True,
    )
    calls: list[str] = []

    response, trace = modules["intent_router"].route_intent_response(
        payload,
        "이 prompt는 호출되면 안 됩니다.",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
    )

    assert calls == []
    assert trace["model_called"] is False
    assert trace["intent_llm_skipped"] is True
    assert json.loads(response) == contract["intent_envelope"]


def test_continuation_request_router_uses_secure_stored_intent_without_calling_model():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    modules = validator._continuation_modules()
    catalog = {
        "table_catalog_items": [
            {"key": "event_index", "payload": {"canonical_columns": ["EVENT_ID"]}},
            {
                "key": "event_detail",
                "payload": {
                    "required_params": ["EVENT_ID"],
                    "canonical_columns": ["EVENT_ID", "STATUS"],
                    "upstream_bindings": [
                        {
                            "entity_type": "event",
                            "source_alias": "previous_result",
                            "source_column": "EVENT_ID",
                            "target_param": "EVENT_ID",
                            "operator": "in",
                        }
                    ],
                },
            },
        ]
    }
    intent = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "event_index", "source_alias": "events", "required_params": {}},
                {"dataset_key": "event_detail", "source_alias": "details", "required_params": {}},
            ],
            "pandas_execution_plan": [
                {"operation": "join", "left_source_alias": "events", "right_source_alias": "details"}
            ],
            "output_contract": {
                "required_columns": ["EVENT_ID", "STATUS"],
                "result_columns": ["EVENT_ID", "STATUS"],
                "grain_columns": ["EVENT_ID"],
                "metric_columns": [],
            },
        }
    }
    compiled_text, _ = modules["compiler"].compile_intent_response(
        {"request": {"question": "이벤트 상세 상태"}},
        json.dumps(intent, ensure_ascii=False),
        catalog,
    )
    stored_plan = json.loads(compiled_text)["intent_plan"]
    stage1_payload = {
        "request": {"session_id": "session-event"},
        "intent_plan": stored_plan,
        "analysis": {"status": "ok"},
        "data": {
            "data_ref": "result:session-event:stage1",
            "row_count": 1,
            "rows": [{"EVENT_ID": "EV-1"}],
            "columns": ["EVENT_ID"],
        },
        "trace": {"inspection": {"result_store": {"status": "ok"}}},
    }
    public = modules["api"]._build_continuation(stage1_payload)
    assert public["status"] == "pending"
    assert "intent_envelope" not in public["continuation_contract"]
    payload = modules["request"].build_request(
        "이벤트 상세 상태",
        session_id="session-event",
        upstream_result_ref="result:session-event:stage1",
        continuation_ref=public["continuation_ref"],
        continuation_contract=public["continuation_contract"],
        skip_intermediate_answer=True,
    )
    stored_document = {
        "session_id": "session-event",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "payload": {
            "intent_plan": stored_plan,
            "analysis": {"status": "ok"},
            "data": {"row_count": 1},
            "storage_manifest": {"result_rows": {"complete": True, "stored_count": 1}},
        },
    }
    calls: list[str] = []

    response, trace = modules["intent_router"].route_intent_response(
        payload,
        "이 prompt는 호출되면 안 됩니다.",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        stored_plan_loader=lambda ref: stored_document,
    )

    assert calls == []
    assert trace["model_called"] is False
    assert trace["intent_llm_skipped"] is True
    assert json.loads(response)["intent_plan"] == stored_plan


@pytest.mark.parametrize("case_id", [1, 5, 8, 11, 30])
def test_continuation_executor_keeps_canonical_semantic_fingerprint(case_id: int):
    manifest = validator.load_manifest()
    manifest_case = next(item for item in manifest["regression_cases"] if item["base_case_id"] == case_id)
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    modules = base.load_flow_modules()
    canonical_v2 = validator.route_validator._v2_modules()
    continuation_modules = validator._continuation_modules()
    base_cases = {int(item["id"]): item for item in base.representative_cases()}
    route_expectations = validator.route_validator.load_route_manifest()

    result = validator.validate_regression_case(
        manifest_case,
        base_cases,
        route_expectations,
        modules,
        canonical_v2,
        continuation_modules,
        "20260701",
    )

    assert result["status"] == "ok", result["errors"]
    assert result["child_calls"] == 1


def test_fingerprint_ignores_prose_code_analysis_kind_and_unspecified_row_order():
    left = {
        "intent_plan": {
            "analysis_kind": "left_name",
            "retrieval_jobs": [{"source_alias": "x", "dataset_key": "d", "required_params": {}, "filters": {}}],
            "temporal_semantics": [{"source_alias": "x", "dataset_key": "d", "query_date": "20260808"}],
            "output_contract": {"grain_columns": ["ID"], "metric_columns": ["QTY"]},
            "decision_reason": ["left prose"],
        },
        "analysis": {"status": "ok"},
        "data": {"columns": ["ID", "QTY"], "rows": [{"ID": "B", "QTY": 2}, {"ID": "A", "QTY": 1}]},
        "answer_message": "left answer",
        "generated_code": "left",
    }
    right = deepcopy(left)
    right["intent_plan"]["analysis_kind"] = "right_name"
    right["intent_plan"]["decision_reason"] = ["right prose"]
    right["data"]["rows"].reverse()
    right["answer_message"] = "right answer"
    right["generated_code"] = "right"

    assert validator.canonical_semantic_fingerprint(left) == validator.canonical_semantic_fingerprint(right)


def test_actual_api_and_router_contract_accepts_non_continuation_single_stage():
    modules = validator._continuation_modules()
    payload = {
        "request": {"session_id": "session-single"},
        "analysis": {"status": "ok"},
        "data": {"row_count": 1, "columns": ["VALUE"], "rows": [{"VALUE": 1}]},
        "answer_message": "단일 분석 완료",
    }
    api_response = modules["api"].build_api_response(payload, payload["answer_message"])

    projection, errors = validator._validate_real_router_projection(
        "단일 분석",
        [api_response],
        validator.load_manifest()["limits"],
    )

    assert api_response["continuation"] == {"status": "not_applicable"}
    assert errors == []
    assert projection["child_calls"] == 1
    assert projection["observation"]["continuation_execution"]["status"] == "ok"


def test_actual_api_and_router_contract_aligns_two_stage_indexes_and_refs():
    modules = validator._continuation_modules()
    dependent = {
        "version": "1",
        "plan_id": "drp-contract-test",
        "plan_hash": "abcdef123456",
        "max_stages": 2,
        "active_stage_index": 0,
        "stages": [
            {"stage_id": "current_hold", "input_bindings": []},
            {
                "stage_id": "latest_history",
                "input_bindings": [
                    {
                        "source_alias": "upstream_result",
                        "source_column": "LOT_ID",
                        "target_param": "LOT_ID",
                        "operator": "in",
                    }
                ],
            },
        ],
        "runtime": {
            "status": "pending",
            "active_stage_index": 0,
            "current_stage_id": "current_hold",
            "next_stage_id": "latest_history",
            "intent_llm_skipped": False,
        },
    }
    first_payload = {
        "request": {"session_id": "session-two-stage"},
        "intent_plan": {"dependent_retrieval_plan": deepcopy(dependent)},
        "analysis": {"status": "ok"},
        "data": {
            "data_ref": "result:session-two-stage:first",
            "row_count": 1,
            "columns": ["LOT_ID"],
            "rows": [{"LOT_ID": "LOT-A"}],
        },
        "trace": {"inspection": {"result_store": {"status": "ok"}}},
        "answer_message": "",
    }
    first = modules["api"].build_api_response(first_payload, "")
    completed = deepcopy(dependent)
    completed["active_stage_index"] = 1
    completed["runtime"] = {
        "status": "complete",
        "active_stage_index": 1,
        "current_stage_id": "latest_history",
        "next_stage_id": "",
        "intent_llm_skipped": True,
    }
    second_payload = {
        "request": {"session_id": "session-two-stage"},
        "intent_plan": {"dependent_retrieval_plan": completed},
        "analysis": {"status": "ok"},
        "data": {
            "data_ref": "result:session-two-stage:second",
            "row_count": 1,
            "columns": ["LOT_ID", "HOLD_DESC"],
            "rows": [{"LOT_ID": "LOT-A", "HOLD_DESC": "최신 사유"}],
        },
        "answer_message": "최신 HOLD 사유 조회 완료",
    }
    second = modules["api"].build_api_response(second_payload, second_payload["answer_message"])

    projection, errors = validator._validate_real_router_projection(
        "현재 HOLD LOT과 최신 사유",
        [first, second],
        validator.load_manifest()["limits"],
    )

    assert first["continuation"]["stage_index"] == 1
    assert second["continuation"]["stage_index"] == 2
    assert errors == []
    assert projection["child_calls"] == 2
    execution = projection["observation"]["continuation_execution"]
    assert execution["status"] == "ok"
    assert execution["auto_continued"] is True


def test_continuation_exports_embed_the_public_contracts():
    result = validator.validate_export_contracts()
    assert result["status"] == "ok", result["errors"]


def test_additive_bundle_keeps_base_01_and_06_byte_identical(tmp_path):
    base_dir = tmp_path / "base"
    continuation_dir = tmp_path / "continuation"
    build_base_bundle(base_dir)
    build_base_bundle(continuation_dir)
    protected_before = {
        filename: (continuation_dir / filename).read_bytes()
        for filename in (
            "01_data_analysis_flow_v2_standalone.json",
            "06_agent_tool_router_flow_v5_standalone.json",
        )
    }
    result = build_continuation_bundle(continuation_dir)

    assert result["flow_count"] == 9
    for filename in (
        "01_data_analysis_flow_v2_standalone.json",
        "06_agent_tool_router_flow_v5_standalone.json",
    ):
        assert (base_dir / filename).read_bytes() == (continuation_dir / filename).read_bytes()
        assert protected_before[filename] == (continuation_dir / filename).read_bytes()

    manifest = json.loads((continuation_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [item["order"] for item in manifest["flows"]] == list(range(1, 10))
    assert manifest["flows"][-2]["file"] == "08_data_analysis_flow_v2_continuation_standalone.json"
    assert manifest["flows"][-1]["file"] == "09_agent_tool_router_continuation_flow_v5_standalone.json"
    assert manifest["continuation_routing_contract"]["base_flows_unchanged"] == [
        "01. v5_data_analysis",
        "06. v5_agent_tool_router",
    ]
    combined = json.loads(
        (continuation_dir / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(combined["flows"]) == 9
    component_count = sum(
        1
        for flow in combined["flows"]
        for node in flow.get("data", {}).get("nodes", [])
        if node.get("data", {}).get("node", {}).get("template", {}).get("_type") == "Component"
        and isinstance(
            node.get("data", {}).get("node", {}).get("template", {}).get("code"),
            dict,
        )
    )
    assert component_count == 160
    expected_runtime_claim = (
        "160/160 passed across 9 flows in "
        "Langflow 1.9.2 / Langflow Base 0.9.2 / LFX 0.4.2"
    )
    assert manifest["validation"]["langflow_lfx_node_templates"] == expected_runtime_claim
    readme = (continuation_dir / "README_IMPORT.md").read_text(encoding="utf-8")
    assert "node template: 9개 Flow 160/160 검증 통과" in readme


def test_additive_bundle_fails_if_base_regeneration_rewrites_protected_flow(tmp_path, monkeypatch):
    output_dir = tmp_path / "continuation"
    build_base_bundle(output_dir)
    real_build = continuation_bundle.base.build_bundle

    def mutated_build(target):
        result = real_build(target)
        protected = Path(target) / "01_data_analysis_flow_v2_standalone.json"
        protected.write_bytes(protected.read_bytes() + b" ")
        return result

    monkeypatch.setattr(continuation_bundle.base, "build_bundle", mutated_build)

    with pytest.raises(ValueError, match="Base regeneration modified protected artifact"):
        build_continuation_bundle(output_dir)
