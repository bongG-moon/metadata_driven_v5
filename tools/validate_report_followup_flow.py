#!/usr/bin/env python3
"""Validate Report 07 -> stored views -> Flow 10 with live Gemini and MongoDB.

The validator uses deterministic dummy Report rows and uniquely named temporary
MongoDB collections.  It never calls Oracle/Goodocs and removes the temporary
documents unless ``--keep-records`` is supplied.  Prompt text, API keys, raw LLM
responses, and source rows are not written to the result JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import types
import uuid
from contextlib import contextmanager
from copy import deepcopy
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_representative_questions as base  # noqa: E402


REPORT_FLOW = ROOT / "langflow_components" / "realtime_production_report_flow"
FOLLOWUP_FLOW = ROOT / "langflow_components" / "report_followup_flow"
DATA_FLOW = ROOT / "langflow_components" / "data_analysis_flow"
SESSION_FLOW = ROOT / "langflow_components" / "session_state_flow"
PRODUCT_KEYS = ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


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


@contextmanager
def _isolated_lfx_stubs():
    """Load standalone helper functions without mutating the installed LFX."""

    class Component:
        pass

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(self, text="", files=None):
            self.text = text
            self.files = list(files or [])
            self.error = False
            self.category = "message"

    module_names = (
        "lfx",
        "lfx.custom",
        "lfx.custom.custom_component",
        "lfx.custom.custom_component.component",
        "lfx.io",
        "lfx.schema",
        "lfx.schema.data",
        "lfx.schema.message",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    modules = {name: types.ModuleType(name) for name in module_names}
    for name, module in modules.items():
        is_package = name in {"lfx", "lfx.custom", "lfx.custom.custom_component", "lfx.schema"}
        module.__spec__ = ModuleSpec(name, loader=None, is_package=is_package)
        if is_package:
            module.__path__ = []
        sys.modules[name] = module
    modules["lfx.custom.custom_component.component"].Component = Component
    for name in (
        "BoolInput",
        "DataInput",
        "DropdownInput",
        "HandleInput",
        "IntInput",
        "MessageTextInput",
        "ModelInput",
        "MultilineInput",
        "Output",
        "SecretStrInput",
        "SliderInput",
        "StrInput",
    ):
        setattr(modules["lfx.io"], name, InputBase)
    modules["lfx.schema.data"].Data = Data
    modules["lfx.schema.message"].Message = Message
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _runtime_modules() -> dict[str, Any]:
    with _isolated_lfx_stubs():
        return {
            "report_dummy": base.load_module(REPORT_FLOW / "00_dummy_production_judgement_data.py"),
            "context_builder": base.load_module(REPORT_FLOW / "00d_report_context_payload_builder.py"),
            "report_builder": base.load_module(REPORT_FLOW / "01_realtime_production_report_builder.py"),
            "result_store": base.load_module(DATA_FLOW / "23_mongodb_result_store.py"),
            "result_loader": base.load_module(DATA_FLOW / "05_mongodb_result_loader.py"),
            "session_loader": base.load_module(SESSION_FLOW / "00_mongodb_session_state_loader.py"),
            "session_writer": base.load_module(SESSION_FLOW / "01_mongodb_session_state_writer.py"),
            "prompt_builder": base.load_module(FOLLOWUP_FLOW / "00_report_followup_prompt_builder.py"),
            "guarded_plan_router": base.load_module(
                FOLLOWUP_FLOW / "00b_report_followup_guarded_plan_router.py"
            ),
            "plan_normalizer": base.load_module(FOLLOWUP_FLOW / "01_report_followup_plan_normalizer.py"),
            "executor": base.load_module(FOLLOWUP_FLOW / "02_report_snapshot_executor.py"),
            "response_builder": base.load_module(FOLLOWUP_FLOW / "03_report_followup_response_builder.py"),
            "terminal": base.load_module(FOLLOWUP_FLOW / "04_report_followup_api_terminal.py"),
        }


def _fake_report_publisher(**_kwargs: Any) -> dict[str, Any]:
    return {
        "report_id": "flow10-live-validation",
        "view_url": "https://validation.invalid/report/view",
        "download_url": "https://validation.invalid/report/download",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "ttl_hours": 4,
        "storage_backend": "validation_stub",
    }


def _run_followup_turn(
    *,
    modules: dict[str, Any],
    question: str,
    session_id: str,
    llm_config: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    result_collection: str,
    session_collection: str,
) -> dict[str, Any]:
    loaded = modules["session_loader"].load_session_state(
        question=_message(question, session_id),
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        session_collection_name=session_collection,
        enabled="true",
        runtime_session_id=session_id,
    )
    load_status = loaded.get("session_state_load", {})
    _require(load_status.get("loaded") is True, f"session state load failed: {load_status}")

    request = modules["prompt_builder"].build_report_followup_request(
        _message(question, session_id),
        loaded,
    )
    _require(
        request.get("execution_gate", {}).get("status") == "ready",
        f"Flow10 entry gate failed: {request.get('trace', {}).get('errors')}",
    )
    prompt = modules["prompt_builder"].build_report_followup_prompt(request)
    _require(bool(prompt.strip()), "Flow10 planning prompt is empty")

    llm_started = time.perf_counter()
    llm_response, plan_route = modules["guarded_plan_router"].route_report_followup_plan_response(
        request,
        prompt,
        lambda rendered_prompt: base.call_llm(rendered_prompt, llm_config),
    )
    llm_latency_seconds = time.perf_counter() - llm_started
    _require(plan_route.get("model_called") is True, f"Flow10 ready plan did not call the model: {plan_route}")
    normalized = modules["plan_normalizer"].normalize_report_followup_plan(request, llm_response)
    plan = normalized.get("intent_plan", {})
    _require(
        normalized.get("execution_gate", {}).get("status") == "ready",
        f"Flow10 plan validation failed: {normalized.get('trace', {}).get('errors')}",
    )
    _require(plan.get("retrieval_jobs") == [], "Flow10 unexpectedly created a retrieval job")
    _require(plan.get("reuse_strategy") == "previous_source", "Flow10 did not select previous_source")

    restored = modules["result_loader"].load_previous_result(
        normalized,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=result_collection,
    )
    restore_status = _inspection(restored, "result_loader")
    _require(restore_status.get("status") == "ok", f"Report view restore failed: {restore_status}")
    executed = modules["executor"].execute_report_snapshot(restored)
    _require(
        executed.get("analysis", {}).get("status") == "ok",
        f"Flow10 deterministic execution failed: {executed.get('trace', {}).get('errors')}",
    )
    response = modules["response_builder"].build_report_followup_response(executed, 10)
    _require(response.get("status") == "ok", f"Flow10 response failed: {response.get('errors')}")
    written = modules["session_writer"].write_session_state(
        response,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        session_collection_name=session_collection,
        enabled="true",
    )
    write_status = written.get("session_state_write", {})
    _require(write_status.get("saved") is True, f"Flow10 session CAS write failed: {write_status}")
    public = modules["terminal"].public_report_followup_response(written)
    _require(public.get("status") == "ok", f"Flow10 terminal failed: {public.get('errors')}")
    rows = executed.get("_full_result_rows") if isinstance(executed.get("_full_result_rows"), list) else []
    return {
        "question": question,
        "rows": deepcopy(rows),
        "plan": deepcopy(plan.get("report_execution_plan") or {}),
        "retrieval_job_count": len(plan.get("retrieval_jobs") or []),
        "result_loader_mode": restore_status.get("mode"),
        "session_turn_count": write_status.get("turn_count"),
        "session_guarded": write_status.get("guarded"),
        "guarded_plan_model_called": plan_route.get("model_called"),
        "llm_latency_seconds": round(llm_latency_seconds, 3),
        "public_status": public.get("status"),
    }


def validate_live_flow10(*, keep_records: bool = False) -> dict[str, Any]:
    base.load_dotenv(ROOT / ".env")
    mongo_uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or "").strip()
    _require(bool(mongo_uri), "MONGODB_URI or MONGO_URL is required")
    mongo_database = str(os.getenv("MONGODB_DATABASE") or "datagov").strip() or "datagov"
    llm_config = base.resolve_llm_config()
    modules = _runtime_modules()
    skipped_model_calls: list[str] = []
    _, blocked_route = modules["guarded_plan_router"].route_report_followup_plan_response(
        {"report_followup": {"status": "blocked"}, "trace": {"errors": []}},
        "",
        lambda prompt: skipped_model_calls.append(prompt),
    )
    _, handoff_route = modules["guarded_plan_router"].route_report_followup_plan_response(
        {"report_followup": {"status": "handoff_required"}, "trace": {"errors": []}},
        "",
        lambda prompt: skipped_model_calls.append(prompt),
    )
    _require(not skipped_model_calls, "Flow10 non-ready guard unexpectedly invoked the model")
    _require(
        blocked_route.get("model_called") is False and handoff_route.get("model_called") is False,
        "Flow10 non-ready guard did not report model_called=false",
    )
    suffix = uuid.uuid4().hex[:12]
    session_id = f"report-followup-flow10-{suffix}"
    result_collection = f"codex_flow10_results_{suffix}"
    session_collection = f"codex_flow10_sessions_{suffix}"
    cleanup_client = None

    try:
        dataset = modules["report_dummy"].build_dummy_production_dataset(
            row_count=160,
            seed=20260816,
            work_date="2026-08-16",
            snapshot_at="2026-08-16T09:00:00+09:00",
        )
        report_question = "D/A 공정그룹 실시간 생산 분석을 해줘"
        report_message = _message(report_question, session_id)
        context_payload = modules["context_builder"].build_report_context_payload(dataset, report_message)
        shortage_rows = context_payload.get("runtime_sources", {}).get("report_shortage_products", [])
        _require(len(shortage_rows) >= 5, "dummy Report did not produce five shortage products")

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
        _require(store_status.get("status") == "ok", f"Report context store failed: {store_status}")
        report = asyncio.run(
            modules["report_builder"].build_realtime_production_report(
                dataset_value=dataset,
                question_value=report_message,
                context_payload_value=stored_context,
                report_api_url="https://validation.invalid",
                report_publisher_fn=_fake_report_publisher,
                file_token="flow10-live-validation",
            )
        )
        _require(report.get("status") == "ok", f"Report build failed: {report.get('errors')}")
        _require(report.get("followup", {}).get("available") is True, "Report follow-up context unavailable")
        initial_write = modules["session_writer"].write_session_state(
            report,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            session_collection_name=session_collection,
            enabled="true",
        )
        _require(
            initial_write.get("session_state_write", {}).get("saved") is True,
            f"Report session write failed: {initial_write.get('session_state_write')}",
        )

        first = _run_followup_turn(
            modules=modules,
            question="방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘",
            session_id=session_id,
            llm_config=llm_config,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            result_collection=result_collection,
            session_collection=session_collection,
        )
        first_plan = first["plan"]
        _require(first_plan.get("source_alias") == "report_shortage_products", "wrong Report view selected")
        first_sort = [item for item in first_plan.get("operations", []) if item.get("operation") == "sort"]
        first_top = [item for item in first_plan.get("operations", []) if item.get("operation") == "top_n"]
        _require(
            bool(first_sort)
            and first_sort[-1].get("column") == "생산실적달성율"
            and first_sort[-1].get("direction") == "asc",
            f"unexpected sort plan: {first_plan}",
        )
        _require(bool(first_top) and first_top[-1].get("limit") == 5, f"unexpected top-N plan: {first_plan}")
        _require(len(first["rows"]) == 5, f"first Flow10 result is not five rows: {len(first['rows'])}")
        rates = [float(row["생산실적달성율"]) for row in first["rows"]]
        _require(rates == sorted(rates), f"achievement rate is not ascending: {rates}")
        identities = [tuple(row.get(key) for key in PRODUCT_KEYS) for row in first["rows"]]
        _require(len(set(identities)) == len(identities), "Flow10 result contains duplicate product grain")

        second = _run_followup_turn(
            modules=modules,
            question="그중 3개만 보여줘",
            session_id=session_id,
            llm_config=llm_config,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            result_collection=result_collection,
            session_collection=session_collection,
        )
        _require(len(second["rows"]) == 3, f"continued Flow10 result is not three rows: {len(second['rows'])}")
        _require(
            [tuple(row.get(key) for key in PRODUCT_KEYS) for row in second["rows"]] == identities[:3],
            "'그중' did not preserve the previous sorted subset",
        )
        _require(second["plan"].get("inherited_previous_view") is True, "second turn did not inherit its previous view")

        return {
            "status": "ok",
            "runtime": {
                "python": sys.version.split()[0],
                "llm_model": llm_config["model"],
            },
            "report": {
                "snapshot_rows": dataset.get("row_count"),
                "shortage_product_rows": len(shortage_rows),
                "context_stored": store_status.get("status"),
            },
            "turns": [
                {
                    "question": first["question"],
                    "source_alias": first_plan.get("source_alias"),
                    "operations": first_plan.get("operations", []),
                    "result_rows": len(first["rows"]),
                    "retrieval_job_count": first["retrieval_job_count"],
                    "result_loader_mode": first["result_loader_mode"],
                    "session_turn_count": first["session_turn_count"],
                    "session_guarded": first["session_guarded"],
                    "guarded_plan_model_called": first["guarded_plan_model_called"],
                    "llm_latency_seconds": first["llm_latency_seconds"],
                },
                {
                    "question": second["question"],
                    "source_alias": second["plan"].get("source_alias"),
                    "operations": second["plan"].get("operations", []),
                    "inherited_previous_view": second["plan"].get("inherited_previous_view"),
                    "result_rows": len(second["rows"]),
                    "retrieval_job_count": second["retrieval_job_count"],
                    "result_loader_mode": second["result_loader_mode"],
                    "session_turn_count": second["session_turn_count"],
                    "session_guarded": second["session_guarded"],
                    "guarded_plan_model_called": second["guarded_plan_model_called"],
                    "llm_latency_seconds": second["llm_latency_seconds"],
                },
            ],
            "assertions": {
                "same_report_snapshot_only": True,
                "no_oracle_or_goodocs_retrieval": True,
                "physical_report_product_grain": PRODUCT_KEYS,
                "first_turn_sorted_top5": True,
                "second_turn_inherited_top3": True,
                "session_cas_guarded": True,
                "non_ready_paths_skip_llm": True,
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
    parser.add_argument("--keep-records", action="store_true", help="Keep the temporary validation collections.")
    parser.add_argument("--output", default="", help="Write the compact validation result JSON to this path.")
    args = parser.parse_args()
    try:
        result = validate_live_flow10(keep_records=args.keep_records)
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        exit_code = 1
    else:
        exit_code = 0
    output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(output)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
