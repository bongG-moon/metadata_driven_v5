# -*- coding: utf-8 -*-
"""실제 Gemini와 MongoDB 상태 저장소를 사용하는 Data Analysis 멀티턴 검증기."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "langflow_components" / "data_analysis_flow"
SESSION_FLOW = ROOT / "langflow_components" / "session_state_flow"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_representative_questions as flow_validator  # noqa: E402


DEFAULT_QUESTIONS = [
    "오늘 DA공정에서 생산량 상위 3개 제품을 알려줘.",
    "이 제품들에 할당된 현재 장비 대수와 장비 LIST를 제품별로 알려줘.",
    "그중 장비 대수가 가장 많은 제품만 보여줘.",
    "오늘 WB공정에서 생산량 상위 5개 제품을 알려줘.",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "실제 Gemini 의도·pandas 생성과 MongoDB result/session 저장을 "
            "Data Analysis Flow 순서대로 검증합니다."
        )
    )
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="같은 세션에서 순서대로 실행할 질문입니다. 생략하면 기본 4턴 질문셋을 사용합니다.",
    )
    parser.add_argument(
        "--reference-date",
        default="",
        help="검증 기준일 YYYYMMDD. 기본값은 VALIDATION_REFERENCE_DATE 또는 20260701입니다.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="검증용 세션 ID. 비우면 충돌하지 않는 임시 ID를 생성합니다.",
    )
    parser.add_argument(
        "--output",
        default="validation_outputs/data_analysis_multiturn_live.json",
        help="전체 검증 보고서를 저장할 UTF-8 JSON 경로입니다.",
    )
    parser.add_argument(
        "--keep-test-data",
        action="store_true",
        help="검증 후 MongoDB의 임시 session/result 문서를 삭제하지 않습니다.",
    )
    parser.add_argument(
        "--validation-profile",
        choices=["auto", "default", "generic"],
        default="auto",
        help="기본 4턴은 상세 계약을, 사용자 지정 질문은 공통 실행·저장 계약을 검증합니다.",
    )
    args = parser.parse_args()

    flow_validator.load_dotenv(ROOT / ".env")
    flow_validator.install_lfx_stubs()
    modules = _load_modules()
    metadata_context = flow_validator.load_metadata_context(modules)
    llm_config = flow_validator.resolve_llm_config()
    reference_date = (
        str(args.reference_date or "").strip()
        or os.getenv("VALIDATION_REFERENCE_DATE", "").strip()
        or "20260701"
    )
    questions = [
        str(item).strip() for item in (args.question or DEFAULT_QUESTIONS) if str(item).strip()
    ]
    validation_profile = (
        "default"
        if args.validation_profile == "auto" and not args.question
        else "generic"
        if args.validation_profile == "auto"
        else args.validation_profile
    )
    session_id = (
        str(args.session_id or "").strip()
        or f"validation-multiturn-{uuid.uuid4()}"
    )
    mongo = _mongo_config()
    report: dict[str, Any] = {
        "status": "error",
        "mode": "live_llm_dummy_retrieval_real_mongodb_state",
        "environment": {
            "reference_date": reference_date,
            "llm_provider": "gemini",
            "llm_model": str(llm_config.get("model") or ""),
            "retrieval_mode": "dummy",
            "result_store": mongo["result_collection"],
            "session_store": mongo["session_collection"],
        },
        "session_id": session_id,
        "questions": questions,
        "validation_profile": validation_profile,
        "turns": [],
        "mongodb_verification": {},
        "cleanup": {},
        "errors": [],
    }

    try:
        for index, question in enumerate(questions, start=1):
            turn = _execute_turn(
                index=index,
                question=question,
                session_id=session_id,
                reference_date=reference_date,
                modules=modules,
                metadata_context=metadata_context,
                llm_config=llm_config,
                mongo=mongo,
                validation_profile=validation_profile,
            )
            report["turns"].append(turn)
            if turn["status"] != "ok":
                break
        report["mongodb_verification"] = _verify_mongodb_state(
            session_id,
            mongo,
            expected_turns=len(report["turns"]),
        )
        report["errors"] = [
            issue
            for turn in report["turns"]
            for issue in turn.get("issues", [])
        ]
        if report["mongodb_verification"].get("issues"):
            report["errors"].extend(report["mongodb_verification"]["issues"])
        report["status"] = (
            "ok"
            if len(report["turns"]) == len(questions) and not report["errors"]
            else "error"
        )
    except Exception as exc:
        report["errors"].append(
            {
                "type": "multiturn_validation_runtime_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        if args.keep_test_data:
            report["cleanup"] = {"status": "skipped", "reason": "keep_test_data"}
        else:
            report["cleanup"] = _cleanup_test_documents(session_id, mongo)
        _write_report(report, args.output)

    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


def _load_modules() -> dict[str, Any]:
    """기존 단일턴 검증 모듈에 멀티턴 저장·복원 노드만 추가합니다."""

    modules = flow_validator.load_flow_modules()
    modules.update(
        {
            "followup_hint": flow_validator.load_module(
                FLOW / "01e_followup_hint_builder.py"
            ),
            "result_loader": flow_validator.load_module(
                FLOW / "05_mongodb_result_loader.py"
            ),
            "upstream_binder": flow_validator.load_module(
                FLOW / "05a_upstream_entity_parameter_binder.py"
            ),
            "execution_gate": flow_validator.load_module(
                FLOW / "14a_retrieval_execution_gate.py"
            ),
            "result_store": flow_validator.load_module(
                FLOW / "23_mongodb_result_store.py"
            ),
            "runtime_cleanup": flow_validator.load_module(
                FLOW / "24_runtime_payload_cleanup.py"
            ),
            "session_loader": flow_validator.load_module(
                SESSION_FLOW / "00_mongodb_session_state_loader.py"
            ),
            "session_writer": flow_validator.load_module(
                SESSION_FLOW / "01_mongodb_session_state_writer.py"
            ),
        }
    )
    return modules


def _execute_turn(
    *,
    index: int,
    question: str,
    session_id: str,
    reference_date: str,
    modules: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    mongo: dict[str, str],
    validation_profile: str,
) -> dict[str, Any]:
    """한 턴을 실제 Flow의 저장·복원 순서대로 실행합니다."""

    started = time.perf_counter()
    loaded = modules["session_loader"].load_session_state(
        SimpleNamespace(text=question, session_id=session_id),
        mongo_uri=mongo["uri"],
        mongo_database=mongo["database"],
        session_collection_name=mongo["session_collection"],
        enabled="true",
        preview_row_limit="5",
        runtime_session_id=session_id,
    )
    payload = modules["request"].build_request(
        question,
        # Langflow의 loaded_state 출력은 조회 진단 wrapper가 아니라 state 본문만 전달한다.
        previous_state_value=deepcopy(loaded.get("state") or {}),
        session_id=session_id,
    )
    payload["request"]["reference_date"] = reference_date
    payload.setdefault("trace", {}).setdefault("inspection", {})[
        "session_state_load"
    ] = deepcopy(loaded.get("session_state_load") or {})
    payload = modules["followup_hint"].build_followup_hint(payload)

    candidates_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    metadata_candidates = candidates_payload.get(
        "metadata_candidates", candidates_payload
    )
    intent_variables = flow_validator.with_specialized_prompt(
        modules["intent_vars"].build_variables(payload, metadata_candidates)
    )
    intent_prompt = flow_validator.render_prompt(
        FLOW / "03_intent_prompt_template_ko.md",
        intent_variables,
    )
    intent_response = flow_validator.call_llm(intent_prompt, llm_config)
    payload = modules["intent"].normalize_intent_plan(
        payload,
        intent_response,
        candidates_payload,
    )
    payload = modules["hydrator"].hydrate_retrieval_jobs(
        payload,
        metadata_context["table"],
        retrieval_mode="dummy",
    )
    payload = modules["result_loader"].load_previous_result(
        payload,
        mongo_uri=mongo["uri"],
        mongo_database=mongo["database"],
        collection_name=mongo["result_collection"],
    )
    payload = modules["upstream_binder"].bind_upstream_entity_parameters(payload)
    payload = modules["validator"].validate_retrieval_payload(payload)
    routed = modules["router"].route_retrieval_jobs(payload, "dummy")
    retrieved = modules["dummy"].retrieve_dummy_data(routed)
    payload = modules["merger"].merge_source_retrieval_payloads(
        payload, retrieved
    )
    payload = modules["adapter"].build_retrieval_payload(payload)
    payload = modules["execution_gate"].apply_retrieval_execution_gate(payload)

    pandas_variables = modules["pandas_vars"].build_variables(payload)
    pandas_variables = flow_validator.with_selected_helper_code(
        modules, pandas_variables
    )
    pandas_prompt = flow_validator.render_prompt(
        FLOW / "16_pandas_prompt_template_ko.md",
        pandas_variables,
    )
    pandas_response = flow_validator.call_llm(pandas_prompt, llm_config)
    payload = modules["executor"].execute_pandas_with_repair(
        payload,
        pandas_response,
        repair_invoker=lambda prompt: flow_validator.call_llm(
            prompt, llm_config
        ),
        repair_prompt_template=(
            FLOW / "17b_pandas_repair_prompt_template_ko.md"
        ).read_text(encoding="utf-8"),
        function_case_helper_code=str(
            pandas_variables.get("function_case_helper_code") or ""
        ),
    )
    payload = modules["result_store"].store_result(
        payload,
        mongo_uri=mongo["uri"],
        mongo_database=mongo["database"],
        collection_name=mongo["result_collection"],
        download_base_url="",
        ttl_hours="1",
    )

    answer_variables = modules["answer_vars"].build_variables(payload)
    answer_variables["domain_answer_guidance"] = (
        FLOW / "answer_domain_guidance_input_example_ko.md"
    ).read_text(encoding="utf-8")
    answer_prompt = flow_validator.render_prompt(
        FLOW / "19_answer_prompt_template_ko.md",
        answer_variables,
    )
    answer_response = flow_validator.call_llm(answer_prompt, llm_config)
    payload = modules["answer_builder"].build_answer_response(
        payload, answer_response
    )
    payload = modules["session_writer"].write_session_state(
        payload,
        mongo_uri=mongo["uri"],
        mongo_database=mongo["database"],
        session_collection_name=mongo["session_collection"],
        enabled="true",
        preview_row_limit="5",
        history_limit="10",
    )

    inspection = (
        payload.get("trace", {}).get("inspection", {})
        if isinstance(payload.get("trace"), dict)
        else {}
    )
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    result_store = inspection.get("result_store") if isinstance(inspection.get("result_store"), dict) else {}
    result_loader = inspection.get("result_loader") if isinstance(inspection.get("result_loader"), dict) else {}
    pandas_trace = inspection.get("pandas_execution") if isinstance(inspection.get("pandas_execution"), dict) else {}
    repair_trace = inspection.get("pandas_repair") if isinstance(inspection.get("pandas_repair"), dict) else {}
    session_write = (
        payload.get("session_state_write")
        if isinstance(payload.get("session_state_write"), dict)
        else {}
    )
    turn = {
        "turn": index,
        "question": question,
        "status": "error",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "session_state_load": deepcopy(loaded.get("session_state_load") or {}),
        "followup_hint": deepcopy(payload.get("followup_hint") or {}),
        "intent": {
            "analysis_kind": plan.get("analysis_kind", ""),
            "request_scope": plan.get("request_scope", ""),
            "reference_mode": plan.get("reference_mode", ""),
            "reuse_strategy": plan.get("reuse_strategy", ""),
            "retrieval_jobs": deepcopy(plan.get("retrieval_jobs") or []),
            "pandas_execution_plan": deepcopy(
                plan.get("pandas_execution_plan") or []
            ),
            "condition_resolution": deepcopy(plan.get("condition_resolution") or {}),
            "output_contract": deepcopy(plan.get("output_contract") or {}),
        },
        "catalog_hydration": deepcopy(inspection.get("catalog_hydration") or {}),
        "upstream_parameter_binding": deepcopy(
            inspection.get("upstream_parameter_binding") or {}
        ),
        "result_loader": deepcopy(result_loader),
        "source_results": [
            {
                "dataset_key": item.get("dataset_key"),
                "source_alias": item.get("source_alias"),
                "status": item.get("status"),
                "row_count": item.get("row_count"),
            }
            for item in payload.get("source_results", [])
            if isinstance(item, dict)
        ],
        "pandas": {
            "status": analysis.get("status"),
            "row_count": analysis.get("row_count", 0),
            "columns": deepcopy(analysis.get("columns") or []),
            "generated_code": pandas_trace.get("generated_code", ""),
            "repair_attempted": bool(repair_trace.get("attempted")),
            "repair_selected": repair_trace.get("selected", ""),
            "error": deepcopy(analysis.get("error") or {}),
        },
        "data_preview": deepcopy((data.get("rows") or [])[:5]),
        "result_store": {
            "status": result_store.get("status", ""),
            "data_ref": result_store.get("data_ref", ""),
            "errors": deepcopy(result_store.get("errors") or []),
        },
        "session_state_write": deepcopy(session_write),
        "issues": [],
    }
    turn["issues"] = _turn_issues(turn, validation_profile)
    turn["status"] = "ok" if not turn["issues"] else "error"

    cleaned = modules["runtime_cleanup"].release_runtime_payload(
        payload, "generation_0"
    )
    display_message = modules["message_adapter"].build_message(cleaned)
    api_response = modules["api_builder"].build_api_response(
        cleaned, display_message
    )
    turn["api_response"] = {
        "response_type": api_response.get("response_type", ""),
        "data_mode": api_response.get("data_mode", ""),
        "message_preview": str(api_response.get("message") or "")[:500],
    }
    turn["runtime_cleanup"] = deepcopy(
        cleaned.get("trace", {})
        .get("inspection", {})
        .get("runtime_cleanup", {})
    )
    return flow_validator.json_safe(turn)


def _turn_issues(
    turn: dict[str, Any],
    validation_profile: str = "default",
) -> list[dict[str, Any]]:
    """각 턴의 후속 계약과 저장·복원 결과를 검증합니다."""

    index = int(turn.get("turn") or 0)
    intent = turn.get("intent") if isinstance(turn.get("intent"), dict) else {}
    pandas = turn.get("pandas") if isinstance(turn.get("pandas"), dict) else {}
    loader = turn.get("session_state_load") if isinstance(turn.get("session_state_load"), dict) else {}
    result_loader = turn.get("result_loader") if isinstance(turn.get("result_loader"), dict) else {}
    result_store = turn.get("result_store") if isinstance(turn.get("result_store"), dict) else {}
    session_write = turn.get("session_state_write") if isinstance(turn.get("session_state_write"), dict) else {}
    issues: list[dict[str, Any]] = []

    def require(condition: bool, issue_type: str, message: str) -> None:
        if not condition:
            issues.append({"type": issue_type, "turn": index, "message": message})

    default_profile = validation_profile == "default"
    expected = {
        1: ("new_analysis", "none", "none"),
        2: ("followup_requery", "previous_result_rows", "previous_result"),
        3: ("followup_transform", "previous_result_transform", "previous_result"),
        4: ("new_analysis", "none", "none"),
    }.get(index) if default_profile else None
    if expected:
        require(
            intent.get("request_scope") == expected[0],
            "unexpected_request_scope",
            f"request_scope={intent.get('request_scope')!r}, expected={expected[0]!r}",
        )
        require(
            intent.get("reference_mode") == expected[1],
            "unexpected_reference_mode",
            f"reference_mode={intent.get('reference_mode')!r}, expected={expected[1]!r}",
        )
        require(
            intent.get("reuse_strategy") == expected[2],
            "unexpected_reuse_strategy",
            f"reuse_strategy={intent.get('reuse_strategy')!r}, expected={expected[2]!r}",
        )
    require(
        pandas.get("status") == "ok",
        "pandas_execution_failed",
        f"pandas status={pandas.get('status')!r}: {pandas.get('error')}",
    )
    row_count = int(pandas.get("row_count") or 0)
    if default_profile and index in {1, 2}:
        require(
            row_count == 3,
            "unexpected_product_row_count",
            f"{index}턴 제품 결과는 3행이어야 하지만 {row_count}행입니다.",
        )
    elif default_profile and index == 3:
        require(
            row_count == 1,
            "unexpected_transform_row_count",
            f"3턴 최다 장비 제품 결과는 1행이어야 하지만 {row_count}행입니다.",
        )
    elif default_profile and index == 4:
        require(
            0 < row_count <= 5,
            "unexpected_top_n_row_count",
            f"4턴 상위 5개 결과 행 수가 유효하지 않습니다: {row_count}",
        )
    require(
        bool(str(pandas.get("generated_code") or "").strip()),
        "missing_generated_code",
        "Gemini가 실행 가능한 pandas 코드를 생성하지 않았습니다.",
    )
    require(
        bool(result_store.get("data_ref")),
        "result_not_stored",
        f"result store status={result_store.get('status')!r}",
    )
    require(
        session_write.get("saved") is True,
        "session_state_not_saved",
        f"session state write={session_write}",
    )
    require(
        int(session_write.get("turn_count") or 0) == index,
        "unexpected_turn_count",
        f"turn_count={session_write.get('turn_count')!r}, expected={index}",
    )
    if index == 1:
        require(
            loader.get("source") in {"mongodb_not_found", "empty"},
            "unexpected_initial_state",
            f"첫 턴 session source={loader.get('source')!r}",
        )
        if default_profile:
            require(
                int(pandas.get("row_count") or 0) > 0,
                "empty_first_turn_result",
                "첫 턴 생산량 상위 제품 결과가 비어 있습니다.",
            )
    if index >= 2:
        require(
            loader.get("source") == "mongodb",
            "session_state_not_restored",
            f"session source={loader.get('source')!r}",
        )
    needs_previous_result = intent.get("reference_mode") in {
        "previous_result_rows",
        "previous_result_transform",
    }
    if (default_profile and index in {2, 3}) or (
        not default_profile and needs_previous_result
    ):
        require(
            result_loader.get("status") == "ok",
            "previous_result_not_restored",
            f"result loader status={result_loader.get('status')!r}",
        )
        require(
            "previous_result"
            in [
                str(item.get("source_alias") or "")
                for item in turn.get("source_results", [])
                if isinstance(item, dict)
            ],
            "previous_result_alias_missing",
            "복원 source 목록에 previous_result가 없습니다.",
        )
    if default_profile and index == 2:
        jobs = [
            item
            for item in intent.get("retrieval_jobs", [])
            if isinstance(item, dict)
        ]
        require(
            any(item.get("dataset_key") == "equipment_assign" for item in jobs),
            "equipment_retrieval_missing",
            "장비 할당 신규 조회 작업이 없습니다.",
        )
        columns = [str(item) for item in pandas.get("columns", [])]
        require(
            any("COUNT" in item.upper() or "대수" in item for item in columns),
            "equipment_count_missing",
            f"장비 대수 컬럼이 없습니다: {columns}",
        )
        require(
            any("LIST" in item.upper() or "목록" in item for item in columns),
            "equipment_list_missing",
            f"장비 목록 컬럼이 없습니다: {columns}",
        )
    if index == 3:
        require(
            not intent.get("retrieval_jobs"),
            "unexpected_transform_retrieval",
            "세 번째 턴은 이전 결과 정렬만 해야 하지만 신규 조회가 있습니다.",
        )
        require(
            int(pandas.get("row_count") or 0) == 1,
            "top_transform_row_count",
            f"최대 장비 제품 결과가 1행이 아닙니다: {pandas.get('row_count')}",
        )
    if index == 4:
        require(
            result_loader.get("status") == "skipped",
            "new_analysis_restored_previous_result",
            f"독립 질문에서 result loader status={result_loader.get('status')!r}",
        )
        jobs = [
            item
            for item in intent.get("retrieval_jobs", [])
            if isinstance(item, dict)
        ]
        require(
            any(item.get("dataset_key") == "production_today" for item in jobs),
            "wb_new_analysis_retrieval_missing",
            "WB 상위 제품 독립 조회에 production_today 작업이 없습니다.",
        )
    return issues


def _mongo_config() -> dict[str, str]:
    """환경변수에서 실제 결과·세션 저장소 설정을 읽습니다."""

    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is required")
    return {
        "uri": uri,
        "database": os.getenv("MONGODB_DATABASE", "datagov").strip()
        or "datagov",
        "result_collection": os.getenv(
            "MONGODB_RESULT_COLLECTION", "agent_v4_result_store"
        ).strip()
        or "agent_v4_result_store",
        "session_collection": os.getenv(
            "MONGODB_SESSION_STATE_COLLECTION", "agent_v4_session_states"
        ).strip()
        or "agent_v4_session_states",
    }


def _verify_mongodb_state(
    session_id: str,
    mongo: dict[str, str],
    *,
    expected_turns: int,
) -> dict[str, Any]:
    """최종 MongoDB session/result 문서를 직접 재조회해 저장 결과를 확인합니다."""

    from pymongo import MongoClient

    client = MongoClient(mongo["uri"], serverSelectionTimeoutMS=5000)
    try:
        database = client[mongo["database"]]
        session_doc = database[mongo["session_collection"]].find_one(
            {"_id": f"session_state:{session_id}"},
            {
                "_id": 1,
                "session_id": 1,
                "turn_count": 1,
                "state.current_data.data_ref": 1,
                "state.last_intent_plan.request_scope": 1,
                "state.last_intent_plan.reference_mode": 1,
            },
        )
        result_docs = list(
            database[mongo["result_collection"]].find(
                {"session_id": session_id},
                {"_id": 1, "question": 1, "session_id": 1},
            )
        )
        issues = []
        if not isinstance(session_doc, dict):
            issues.append(
                {
                    "type": "session_document_missing",
                    "message": "최종 세션 문서를 MongoDB에서 찾지 못했습니다.",
                }
            )
        elif int(session_doc.get("turn_count") or 0) != expected_turns:
            issues.append(
                {
                    "type": "session_document_turn_count_mismatch",
                    "message": (
                        f"MongoDB turn_count={session_doc.get('turn_count')!r}, "
                        f"expected={expected_turns}"
                    ),
                }
            )
        if len(result_docs) != expected_turns:
            issues.append(
                {
                    "type": "result_document_count_mismatch",
                    "message": (
                        f"MongoDB result docs={len(result_docs)}, "
                        f"expected={expected_turns}"
                    ),
                }
            )
        return flow_validator.json_safe(
            {
                "session_document": session_doc or {},
                "result_documents": result_docs,
                "result_document_count": len(result_docs),
                "issues": issues,
            }
        )
    finally:
        client.close()


def _cleanup_test_documents(
    session_id: str,
    mongo: dict[str, str],
) -> dict[str, Any]:
    """현재 검증이 만든 정확한 세션 ID의 문서만 삭제합니다."""

    from pymongo import MongoClient

    client = MongoClient(mongo["uri"], serverSelectionTimeoutMS=5000)
    try:
        database = client[mongo["database"]]
        session_deleted = database[mongo["session_collection"]].delete_one(
            {"_id": f"session_state:{session_id}"}
        )
        results_deleted = database[mongo["result_collection"]].delete_many(
            {"session_id": session_id}
        )
        return {
            "status": "ok",
            "session_deleted_count": int(session_deleted.deleted_count),
            "result_deleted_count": int(results_deleted.deleted_count),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
        }
    finally:
        client.close()


def _write_report(report: dict[str, Any], output: str) -> None:
    """민감한 연결값을 제외한 전체 검증 보고서를 UTF-8 JSON으로 저장합니다."""

    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(flow_validator.json_safe(report), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    """터미널에는 턴별 핵심 상태와 오류만 짧게 출력합니다."""

    return {
        "status": report.get("status"),
        "mode": report.get("mode"),
        "session_id": report.get("session_id"),
        "turns": [
            {
                "turn": item.get("turn"),
                "status": item.get("status"),
                "request_scope": item.get("intent", {}).get("request_scope"),
                "reference_mode": item.get("intent", {}).get("reference_mode"),
                "reuse_strategy": item.get("intent", {}).get("reuse_strategy"),
                "row_count": item.get("pandas", {}).get("row_count"),
                "result_loader": item.get("result_loader", {}).get("status"),
                "session_source": item.get("session_state_load", {}).get(
                    "source"
                ),
                "turn_count": item.get("session_state_write", {}).get(
                    "turn_count"
                ),
                "issues": item.get("issues"),
            }
            for item in report.get("turns", [])
        ],
        "mongodb_verification": {
            "result_document_count": report.get(
                "mongodb_verification", {}
            ).get("result_document_count"),
            "issues": report.get("mongodb_verification", {}).get("issues"),
        },
        "cleanup": report.get("cleanup"),
        "errors": report.get("errors"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
