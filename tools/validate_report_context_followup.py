#!/usr/bin/env python3
"""Run a live Report -> stored snapshot -> Flow 01 follow-up validation.

The validator uses the configured MongoDB and Gemini credentials, but writes
only to uniquely named temporary collections and removes those collections at
the end.  It deliberately does not call an Oracle/Goodocs source: the scenario
under test must answer from the immutable Report snapshot without retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_representative_questions as base  # noqa: E402


FLOW = ROOT / "langflow_components" / "data_analysis_flow"
V2_FLOW = ROOT / "langflow_components" / "data_analysis_flow_v2"
REPORT_FLOW = ROOT / "langflow_components" / "realtime_production_report_flow"
SESSION_FLOW = ROOT / "langflow_components" / "session_state_flow"


def _message(text: str, session_id: str) -> Any:
    return SimpleNamespace(
        text=text,
        session_id=session_id,
        data={"text": text, "session_id": session_id},
    )


def _inspection(payload: dict[str, Any], key: str) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    value = inspection.get(key)
    return value if isinstance(value, dict) else {}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _runtime_modules() -> dict[str, Any]:
    modules = base.load_flow_modules()
    modules.update(
        {
            "followup_hint": base.load_module(FLOW / "01e_followup_hint_builder.py"),
            "result_loader": base.load_module(FLOW / "05_mongodb_result_loader.py"),
            "result_store": base.load_module(FLOW / "23_mongodb_result_store.py"),
            "resolver": base.load_module(V2_FLOW / "14b_simple_analysis_contract_resolver.py"),
            "selection": base.load_module(V2_FLOW / "15_function_case_selection_builder.py"),
            "view_bundle_builder": base.load_module(REPORT_FLOW / "00d_report_context_payload_builder.py"),
            "context_publisher": base.load_module(REPORT_FLOW / "00e_report_context_publisher.py"),
            "report_builder": base.load_module(REPORT_FLOW / "01_realtime_production_report_builder.py"),
            "report_dummy": base.load_module(REPORT_FLOW / "00_dummy_production_judgement_data.py"),
            "session_writer": base.load_module(SESSION_FLOW / "01_mongodb_session_state_writer.py"),
            "session_loader": base.load_module(SESSION_FLOW / "00_mongodb_session_state_loader.py"),
        }
    )
    return modules


def _fake_report_publisher(**_kwargs: Any) -> dict[str, Any]:
    return {
        "report_id": "live-report-context-validation",
        "view_url": "https://validation.invalid/report/view",
        "download_url": "https://validation.invalid/report/download",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "ttl_hours": 4,
        "storage_backend": "validation_stub",
    }


def validate_live_followup(*, keep_records: bool = False) -> dict[str, Any]:
    base.load_dotenv(ROOT / ".env")
    base.install_lfx_stubs()
    mongo_uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or "").strip()
    _require(bool(mongo_uri), "MONGODB_URI or MONGO_URL is required")
    mongo_database = str(os.getenv("MONGODB_DATABASE") or "datagov").strip() or "datagov"
    suffix = uuid.uuid4().hex[:12]
    session_id = f"report-context-validation-{suffix}"
    result_collection = f"codex_report_context_results_{suffix}"
    session_collection = f"codex_report_context_sessions_{suffix}"
    modules = _runtime_modules()
    llm_config = base.resolve_llm_config()

    cleanup_client = None
    try:
        dataset = modules["report_dummy"].build_dummy_production_dataset(
            row_count=120,
            seed=20260815,
            work_date="2026-08-15",
            snapshot_at="2026-08-15T09:00:00+09:00",
        )
        report_question = "D/A 공정그룹 실시간 생산 분석을 해줘"
        report_message = _message(report_question, session_id)
        report_bundle = modules["view_bundle_builder"].build_realtime_report_view_bundle(dataset, report_message)
        context_payload = modules["context_publisher"].build_report_context_payload(report_message, report_bundle)
        stored_context = modules["result_store"].store_result(
            context_payload,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_name=result_collection,
            download_base_url="http://127.0.0.1:5000",
            ttl_hours="4",
            max_result_rows="20000",
            max_source_rows_per_alias="10000",
        )
        store_status = _inspection(stored_context, "result_store")
        _require(store_status.get("status") == "ok", f"result store failed: {store_status}")

        report = asyncio.run(
            modules["report_builder"].build_realtime_production_report(
                dataset_value=dataset,
                question_value=report_message,
                context_payload_value=stored_context,
                report_api_url="https://validation.invalid",
                report_publisher_fn=_fake_report_publisher,
                file_token="live-report-context-validation",
            )
        )
        _require(report.get("status") == "ok", f"report build failed: {report.get('errors')}")
        _require(report.get("followup", {}).get("available") is True, "report follow-up context is unavailable")

        written = modules["session_writer"].write_session_state(
            report,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            session_collection_name=session_collection,
            enabled="true",
        )
        write_status = written.get("session_state_write", {})
        _require(write_status.get("saved") is True, f"session state write failed: {write_status}")

        followup_question = "그중 생산부족 제품만 보여줘"
        loaded = modules["session_loader"].load_session_state(
            question=_message(followup_question, session_id),
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            session_collection_name=session_collection,
            enabled="true",
            runtime_session_id=session_id,
        )
        load_status = loaded.get("session_state_load", {})
        _require(load_status.get("loaded") is True, f"session state load failed: {load_status}")

        payload = modules["request"].build_request(
            followup_question,
            loaded.get("state"),
            session_id=session_id,
        )
        payload = modules["followup_hint"].build_followup_hint(payload)
        hint = payload.get("followup_hint", {})
        _require(hint.get("request_scope_hint") == "followup_transform", f"unexpected hint: {hint}")
        _require(hint.get("fresh_data_requested") is False, f"unexpected refresh hint: {hint}")

        metadata_context = base.load_metadata_context(modules)
        candidates_payload = modules["candidates"].build_metadata_candidates(
            payload,
            metadata_context["domain"],
            metadata_context["table"],
            metadata_context["main"],
        )
        metadata_candidates = candidates_payload.get("metadata_candidates", candidates_payload)
        intent_variables = base.with_specialized_prompt(
            modules["intent_vars"].build_variables(payload, metadata_candidates)
        )
        intent_prompt = base.render_prompt(V2_FLOW / "03_intent_prompt_template_ko.md", intent_variables)
        intent_response = base.call_llm(intent_prompt, llm_config)
        payload = modules["intent"].normalize_intent_plan(
            payload,
            intent_response,
            candidates_payload,
        )
        plan = payload.get("intent_plan", {})
        _require(not plan.get("retrieval_jobs"), f"snapshot follow-up unexpectedly planned retrieval: {plan.get('retrieval_jobs')}")
        _require(plan.get("reuse_strategy") in {"previous_source", "previous_result"}, f"unexpected reuse strategy: {plan}")
        _require(payload.get("execution_gate", {}).get("status") != "blocked", f"intent was blocked: {payload.get('trace', {}).get('errors')}")

        payload = modules["result_loader"].load_previous_result(
            payload,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_name=result_collection,
        )
        result_loader_status = _inspection(payload, "result_loader")
        _require(result_loader_status.get("status") == "ok", f"snapshot restore failed: {result_loader_status}")
        snapshot_rows = payload.get("runtime_sources", {}).get("report_snapshot", [])
        _require(len(snapshot_rows) == dataset.get("row_count"), "restored snapshot row count differs from Report source")

        resolved = modules["resolver"].resolve_simple_analysis_contract(payload)
        selection = modules["selection"].build_function_case_selection_only(resolved)
        helper_library = (FLOW / "function_case_helper_code_input_example.py").read_text(encoding="utf-8")
        helper_code = modules["helper_builder"].build_selected_helper_code(selection, helper_library)
        pandas_prompt = modules["pandas_vars"].build_route_aware_pandas_prompt(
            resolved,
            (FLOW / "16_pandas_prompt_template_ko.md").read_text(encoding="utf-8"),
            helper_code,
        )
        pandas_model_calls: list[str] = []

        def invoke_pandas(prompt: str) -> str:
            pandas_model_calls.append(prompt)
            return base.call_llm(prompt, llm_config)

        executed = modules["executor"].execute_hybrid_analysis(
            resolved,
            pandas_prompt,
            model_invoker=invoke_pandas,
            repair_prompt_template=(FLOW / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8"),
            function_case_helper_code=helper_code,
            max_repair_attempts=1,
        )
        _require(executed.get("analysis", {}).get("status") == "ok", f"snapshot analysis failed: {executed.get('trace', {}).get('errors')}")
        result_rows = executed.get("_full_result_rows")
        if not isinstance(result_rows, list):
            result_rows = executed.get("data", {}).get("rows", [])
        expected_rows = [
            row
            for row in dataset.get("rows", [])
            if isinstance(row, dict) and row.get("달성율*판정") == "생산부족"
        ]
        _require(
            len(result_rows) == len(expected_rows),
            "snapshot result mismatch: "
            + json.dumps(
                {
                    "expected_rows": len(expected_rows),
                    "actual_rows": len(result_rows),
                    "intent_plan": {
                        "analysis_kind": plan.get("analysis_kind"),
                        "request_scope": plan.get("request_scope"),
                        "reference_mode": plan.get("reference_mode"),
                        "reuse_strategy": plan.get("reuse_strategy"),
                        "retrieval_jobs": plan.get("retrieval_jobs"),
                        "pandas_execution_plan": plan.get("pandas_execution_plan"),
                        "output_contract": plan.get("output_contract"),
                    },
                    "analysis": {
                        "status": executed.get("analysis", {}).get("status"),
                        "execution_route": executed.get("analysis", {}).get("execution_route"),
                        "analysis_execution_mode": executed.get("analysis", {}).get("analysis_execution_mode"),
                        "row_count": executed.get("analysis", {}).get("row_count"),
                    },
                    "result_preview": result_rows[:3],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        _require(
            all(isinstance(row, dict) and row.get("달성율*판정") == "생산부족" for row in result_rows),
            "result contains a row outside the Report shortage subset",
        )

        answer_prompt = modules["answer_vars"].build_route_aware_answer_prompt(
            executed,
            (FLOW / "19_answer_prompt_template_ko.md").read_text(encoding="utf-8"),
            (FLOW / "answer_domain_guidance_input_example_ko.md").read_text(encoding="utf-8"),
        )
        answer_text = base.call_llm(answer_prompt, llm_config) if answer_prompt else ""
        answered = modules["answer_builder"].build_answer_response(executed, answer_text)
        _require(bool(str(answered.get("answer_message") or "").strip()), "final answer message is empty")

        return {
            "status": "ok",
            "runtime": {
                "python": sys.version.split()[0],
                "llm_model": llm_config["model"],
            },
            "context": {
                "contract_version": report.get("state", {}).get("current_data", {}).get("report_context", {}).get("context_version"),
                "snapshot_rows": dataset.get("row_count"),
                "stored": store_status.get("status"),
                "session_saved": write_status.get("saved"),
            },
            "followup": {
                "question": followup_question,
                "request_scope": plan.get("request_scope"),
                "reference_mode": plan.get("reference_mode"),
                "reuse_strategy": plan.get("reuse_strategy"),
                "retrieval_job_count": len(plan.get("retrieval_jobs") or []),
                "result_loader_mode": result_loader_status.get("mode"),
                "result_rows": len(result_rows),
                "expected_rows": len(expected_rows),
                "all_rows_from_report_snapshot": True,
                "planned_steps": plan.get("pandas_execution_plan") or [],
                "plan_filter_fields": {
                    key: value
                    for key, value in plan.items()
                    if any(token in str(key).casefold() for token in ("filter", "condition", "criteria"))
                },
                "resolved_steps": (
                    resolved.get("intent_plan", {}).get("pandas_execution_plan", [])
                    if isinstance(resolved.get("intent_plan"), dict)
                    else []
                ),
            },
            "model_calls": {
                "intent": 1,
                "pandas_generation": len(pandas_model_calls),
                "answer": 1 if answer_prompt else 0,
            },
        }
    finally:
        if not keep_records:
            try:
                from pymongo import MongoClient

                cleanup_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
                cleanup_client[mongo_database].drop_collection(result_collection)
                cleanup_client[mongo_database].drop_collection(session_collection)
            finally:
                if cleanup_client is not None:
                    cleanup_client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-records", action="store_true", help="Keep the uniquely named validation collections.")
    args = parser.parse_args()
    try:
        result = validate_live_followup(keep_records=args.keep_records)
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
