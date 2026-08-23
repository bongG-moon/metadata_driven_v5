from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
import types
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
# The representative fixture follows the supported V2 execution components.
# Shared retrieval components remain in ``FLOW``; V2 owns planning, hybrid
# pandas execution, answer construction, and message rendering.
FLOW = ROOT / "langflow_components" / "data_analysis_flow"
V2_FLOW = ROOT / "langflow_components" / "data_analysis_flow_v2"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import data_analysis_semantic_validator as semantic_validator  # noqa: E402

PRODUCT_KEYS = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO", "DEVICE"]
TARGET_PRODUCT_KEYS = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
DA_PROCESSES = ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]
WB_PROCESSES = ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"]
FCB_PROCESSES = ["FCB1", "FCB2", "FCB/H"]
BG_PROCESSES = ["B/G1", "B/G2"]
MOBILE_PKGS = ["LFBGA", "TFBGA", "UFBGA", "VFBGA", "WFBGA"]

# These operational questions depend on source datasets that are intentionally
# not registered in the current production Table Catalog.  Keep their stable
# IDs in the source fixture for historical comparison, but do not include them
# in the active representative run until the corresponding Catalog contracts
# are registered again.
EXCLUDED_OPERATIONAL_CASE_IDS = {5, 24, 25}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Langflow-like validation for the representative manufacturing question set.")
    parser.add_argument("--json", action="store_true", help="Print full validation result as JSON.")
    parser.add_argument("--use-llm", action="store_true", help="Use .env MongoDB metadata and LLM settings to run prompt -> LLM -> flow validation.")
    parser.add_argument("--limit", type=int, default=0, help="Validate only the first N cases.")
    parser.add_argument("--ids", default="", help="Comma-separated case ids to validate, for example: 3,8,13.")
    parser.add_argument("--reference-date", default="", help="Override request.reference_date for this validation run. Defaults to VALIDATION_REFERENCE_DATE or 20260701.")
    parser.add_argument("--output", default="", help="Write the full UTF-8 JSON validation report to this path.")
    parser.add_argument(
        "--validation-profile",
        choices=["auto", semantic_validator.FIXTURE_EXACT, semantic_validator.SEMANTIC_LIVE],
        default="auto",
        help="Use exact fixture comparison or semantic live validation. auto selects semantic_live with --use-llm.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    reference_date = args.reference_date.strip() or os.getenv("VALIDATION_REFERENCE_DATE", "").strip() or "20260701"
    install_lfx_stubs()
    modules = load_flow_modules()
    cases = representative_cases()
    if args.ids.strip():
        selected_ids = {int(item.strip()) for item in args.ids.split(",") if item.strip()}
        cases = [item for item in cases if int(item["id"]) in selected_ids]
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]
    validation_profile = semantic_validator.resolve_validation_profile(
        args.validation_profile,
        use_llm=bool(args.use_llm),
    )
    if args.use_llm:
        metadata_context = load_metadata_context(modules)
        llm_config = resolve_llm_config()
        results = [
            run_llm_case(
                case,
                modules,
                metadata_context,
                llm_config,
                reference_date,
                validation_profile=validation_profile,
            )
            for case in cases
        ]
    else:
        results = [
            run_case(
                case,
                modules,
                reference_date,
                validation_profile=validation_profile,
            )
            for case in cases
        ]
    failed = [item for item in results if item["status"] != "ok"]

    report = {
        "status": "ok" if not failed else "error",
        "validation_profile": validation_profile,
        "results": results,
    }
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in results:
            marker = "OK" if item["status"] == "ok" else "FAIL"
            print(f"[{marker}] {item['id']}. {item['question']}")
            print(f"  intent={item['analysis_kind']} jobs={item['retrieval_job_count']} rows={item['row_count']}")
            print(f"  columns={', '.join(item['columns'])}")
            if item.get("errors"):
                print(f"  errors={item['errors']}")
            if item.get("warnings"):
                print(f"  warnings={item['warnings']}")
        print(f"\nsummary: {len(results) - len(failed)}/{len(results)} passed")

    return 1 if failed else 0


def run_llm_case(
    case: dict[str, Any],
    modules: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    reference_date: str,
    *,
    validation_profile: str = semantic_validator.SEMANTIC_LIVE,
) -> dict[str, Any]:
    payload = build_validation_request(case["question"], modules, reference_date)
    candidates_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    metadata_candidates = candidates_payload.get("metadata_candidates", candidates_payload)
    intent_vars = with_specialized_prompt(modules["intent_vars"].build_variables(payload, metadata_candidates))
    # Match the active V2 Flow: shared execution templates remain under FLOW,
    # while intent planning is owned by the V2 prompt.
    intent_prompt = render_prompt(V2_FLOW / "03_intent_prompt_template_ko.md", intent_vars)
    intent_response = call_llm(intent_prompt, llm_config)
    payload = modules["intent"].normalize_intent_plan(
        payload,
        intent_response,
        candidates_payload,
    )
    payload = modules["hydrator"].hydrate_retrieval_jobs(payload, metadata_context["table"], retrieval_mode="dummy")
    payload = modules["validator"].validate_retrieval_payload(payload)
    dummy_bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    dummy_result = modules["dummy"].retrieve_dummy_data(dummy_bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, dummy_result)
    payload = modules["adapter"].build_retrieval_payload(payload)
    # Match the production Flow order: the 14B resolver decides whether the
    # trusted Fast/Typed contract can execute before any pandas model output is
    # considered.  The live harness still calls the model for replay coverage,
    # while the executor must ignore that code when the resolved contract says
    # the deterministic path is sufficient.
    payload = modules["fast_resolver"].resolve_simple_analysis_contract(payload)

    pandas_vars = modules["pandas_vars"].build_variables(payload)
    pandas_vars = with_selected_helper_code(modules, pandas_vars)
    pandas_prompt = render_prompt(FLOW / "16_pandas_prompt_template_ko.md", pandas_vars)
    pandas_response = call_llm(pandas_prompt, llm_config)
    repair_template = (FLOW / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8")
    selected_payload = modules["executor"].execute_pandas_with_repair(
        payload,
        pandas_response,
        repair_invoker=lambda prompt: call_llm(prompt, llm_config),
        repair_prompt_template=repair_template,
        function_case_helper_code=str(pandas_vars.get("function_case_helper_code") or ""),
    )

    answer_vars = modules["answer_vars"].build_variables(selected_payload)
    answer_vars["domain_answer_guidance"] = (FLOW / "answer_domain_guidance_input_example_ko.md").read_text(encoding="utf-8")
    answer_prompt = render_prompt(FLOW / "19_answer_prompt_template_ko.md", answer_vars)
    answer_response = call_llm(answer_prompt, llm_config)
    selected_payload = modules["answer_builder"].build_answer_response(selected_payload, answer_response)
    display_message = modules["message_adapter"].build_message(selected_payload)
    api_response = modules["api_builder"].build_api_response(selected_payload, display_message)
    return summarize_validation_result(
        case,
        selected_payload,
        pandas_vars,
        validation_profile=validation_profile,
        api_response=api_response,
    )


def run_case(
    case: dict[str, Any],
    modules: dict[str, Any],
    reference_date: str,
    *,
    validation_profile: str = semantic_validator.FIXTURE_EXACT,
) -> dict[str, Any]:
    payload = build_validation_request(case["question"], modules, reference_date)
    payload = modules["intent"].normalize_intent_plan(payload, case["intent_response"])
    payload = modules["hydrator"].hydrate_retrieval_jobs(payload, validation_catalog(case), retrieval_mode="dummy")
    payload = modules["validator"].validate_retrieval_payload(payload)
    dummy_bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    dummy_result = modules["dummy"].retrieve_dummy_data(dummy_bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, dummy_result)
    payload = modules["adapter"].build_retrieval_payload(payload)
    pandas_vars = modules["pandas_vars"].build_variables(payload)
    pandas_vars = with_selected_helper_code(modules, pandas_vars)
    pandas_code = (
        inline_helper_source(case["pandas_code"], str(case.get("helper_function") or ""))
        if case.get("requires_helper")
        else case["pandas_code"]
    )
    payload = modules["executor"].execute_pandas_code(payload, {"code": pandas_code})
    row_count = int(payload.get("data", {}).get("row_count") or 0)
    payload = modules["answer_builder"].build_answer_response(
        payload,
        f"[더미 데이터] '{case['question']}' 분석 결과는 {row_count}건입니다.",
    )
    display_message = modules["message_adapter"].build_message(payload)
    api_response = modules["api_builder"].build_api_response(payload, display_message)
    return summarize_validation_result(
        case,
        payload,
        pandas_vars,
        validation_profile=validation_profile,
        api_response=api_response,
    )


def with_selected_helper_code(modules: dict[str, Any], pandas_vars: dict[str, Any]) -> dict[str, Any]:
    next_vars = deepcopy(pandas_vars)
    selection_text = str(pandas_vars.get("function_case_selection_json") or "{}")
    helper_library = (FLOW / "function_case_helper_code_input_example.py").read_text(encoding="utf-8")
    next_vars["function_case_helper_code"] = modules["helper_builder"].build_selected_helper_code(selection_text, helper_library)
    return next_vars


def with_specialized_prompt(intent_vars: dict[str, Any]) -> dict[str, Any]:
    next_vars = deepcopy(intent_vars)
    if "specialized_prompt" in next_vars:
        return next_vars
    prompt_file = os.getenv("VALIDATION_SPECIALIZED_PROMPT_FILE", "").strip()
    path = Path(prompt_file) if prompt_file else FLOW / "specialized_prompt_input_example_ko.md"
    if path.exists():
        next_vars["specialized_prompt"] = path.read_text(encoding="utf-8")
    else:
        next_vars["specialized_prompt"] = ""
    return next_vars


def inline_helper_source(pandas_code: str, function_name: str = "match_product_tokens") -> str:
    source = function_case_source(function_name or "match_product_tokens")
    return source + "\n\n" + pandas_code if source else pandas_code


def function_case_source(function_name: str = "") -> str:
    source = (FLOW / "function_case_helper_code_input_example.py").read_text(encoding="utf-8")
    if not function_name:
        return source
    tree = ast.parse(source)
    source_lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return "\n".join(source_lines[node.lineno - 1 : node.end_lineno])
    return ""


def build_validation_request(question: str, modules: dict[str, Any], reference_date: str) -> dict[str, Any]:
    payload = modules["request"].build_request(question)
    if reference_date:
        payload.setdefault("request", {})["reference_date"] = reference_date
    return payload


def summarize_validation_result(
    case: dict[str, Any],
    payload: dict[str, Any],
    pandas_vars: dict[str, Any],
    validation_profile: str,
    api_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = semantic_validator.resolve_validation_profile(validation_profile)
    errors: list[str] = []
    warnings: list[str] = []
    diagnostics: list[str] = []
    if payload.get("analysis", {}).get("status") != "ok":
        errors.append(payload.get("analysis", {}).get("error", {}).get("message", "pandas execution failed"))
    if not payload.get("intent_plan", {}).get("retrieval_jobs"):
        errors.append("missing retrieval_jobs")
    if payload.get("analysis", {}).get("row_count", 0) < case.get("min_rows", 1):
        errors.append(f"row_count < {case.get('min_rows', 1)}")
    actual_rows = payload.get("data", {}).get("rows", [])
    exact_differences = semantic_validator.fixture_differences(case, payload)
    if profile == semantic_validator.FIXTURE_EXACT:
        errors.extend(exact_differences)
    else:
        diagnostics.extend(exact_differences)
    forbidden_values = case.get("forbidden_values")
    if isinstance(forbidden_values, dict):
        for column, values in forbidden_values.items():
            blocked_values = values if isinstance(values, list) else [values]
            found = [row.get(column) for row in actual_rows if isinstance(row, dict) and row.get(column) in blocked_values]
            if found:
                errors.append(f"forbidden {column} values present: {found!r}")
    function_case_text = json.dumps(
        {
            "selection": pandas_vars.get("function_case_selection_json", ""),
            "helper_code": pandas_vars.get("function_case_helper_code", ""),
        },
        ensure_ascii=False,
    )
    helper_function = str(case.get("helper_function") or "match_product_tokens")
    if case.get("requires_helper") and helper_function not in function_case_text:
        message = f"missing {helper_function} function case context"
        if profile == semantic_validator.FIXTURE_EXACT:
            errors.append(message)
        else:
            warnings.append(message)
    if (api_response or {}).get("data_mode") != "dummy":
        errors.append("data_mode != 'dummy'")

    semantic_checks = semantic_validator.validate_semantic_payload(
        payload,
        question=str(case.get("question") or ""),
        pandas_variables=pandas_vars,
    )
    expectation_errors = (
        semantic_validator.validate_case_expectation(
            case,
            payload,
            pandas_variables=pandas_vars,
        )
        if profile == semantic_validator.SEMANTIC_LIVE
        else []
    )
    errors.extend(
        f"{item.get('type')}: {item.get('message')}"
        for item in [*semantic_checks.get("errors", []), *expectation_errors]
    )
    warnings.extend(
        f"{item.get('type')}: {item.get('message')}"
        for item in semantic_checks.get("warnings", [])
    )

    pandas_trace = payload.get("trace", {}).get("inspection", {}).get("pandas_execution", {})
    result = {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
        "validation_profile": profile,
        "analysis_kind": payload.get("intent_plan", {}).get("analysis_kind", ""),
        "retrieval_job_count": len(payload.get("intent_plan", {}).get("retrieval_jobs", [])),
        "row_count": payload.get("analysis", {}).get("row_count", 0),
        "columns": payload.get("analysis", {}).get("columns", []),
        "preview_rows": payload.get("data", {}).get("rows", [])[:10],
        "intent_plan": payload.get("intent_plan", {}),
        "source_results": [
            {
                "source_alias": item.get("source_alias"),
                "dataset_key": item.get("dataset_key"),
                "row_count": item.get("row_count"),
                "applied_params": item.get("applied_params"),
                "pandas_filters": item.get("pandas_filters"),
            }
            for item in payload.get("source_results", [])
            if isinstance(item, dict)
        ],
        "generated_code": pandas_trace.get("generated_code", ""),
        "effective_code_with_helpers": pandas_trace.get("effective_code_with_helpers", ""),
        "used_helpers": pandas_trace.get("used_helpers", []),
        "message": (api_response or {}).get("message", ""),
        "data_mode": (api_response or {}).get("data_mode", ""),
        "errors": errors,
        "warnings": warnings,
        "fixture_differences": diagnostics,
        "semantic_checks": semantic_checks,
    }
    return json_safe(result)


def _legacy_representative_cases() -> list[dict[str, Any]]:
    return [
        case(
            1,
            "오늘 투입된 제품중 MCP NO가 L-267로 시작하는 제품의 INPUT 수량 알려줘",
            "input_qty_by_l267_prefix",
            [job("production_today", "production_data", "20260701", {"OPER_NAME": eq("INPUT"), "MCP_NO": {"operator": "starts_with", "value": "L-267"}})],
            code_group_sum("production_data", PRODUCT_KEYS, "PRODUCTION", "INPUT_QTY"),
            ["INPUT_QTY", "MCP_NO", "DEVICE"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV-L267", "MCP_NO": "L-267A1", "INPUT_QTY": 292},
        ),
        case(
            2,
            "어제 DA공정 차수별 생산량 알려줘",
            "da_production_by_step",
            [job("production", "production_data", "20260630", {"OPER_NAME": in_values(DA_PROCESSES)})],
            "df = sources['production_data']\nresult = df.groupby('OPER_NAME', as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'}).sort_values('OPER_NAME')",
            ["OPER_NAME", "TOTAL_PRODUCTION"],
            expected_row_count=6,
            expected_first_row={"OPER_NAME": "D/A1", "TOTAL_PRODUCTION": 1328},
        ),
        case(
            3,
            "어제 Mobile제품의 PKG OUT실적을 제품별로 알려줘",
            "mobile_pkg_out_by_product",
            [job("production", "production_data", "20260630", {"OPER_NAME": eq("PKG OUT")})],
            (
                "df = sources['production_data']\n"
                f"df = df[df['MODE'].astype(str).str.startswith('LP') & df['PKG_TYPE1'].isin({MOBILE_PKGS!r}) & df['MCP_NO'].fillna('').astype(str).eq('')]\n"
                "result = df.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'PKG_OUT_QTY'}).sort_values(['PKG_OUT_QTY', 'DEVICE'], ascending=[False, True])"
            ),
            ["PKG_OUT_QTY", "DEVICE"],
            expected_row_count=2,
            expected_first_row={"DEVICE": "DEV002", "PKG_OUT_QTY": 504},
            expected_rows=[
                {"DEVICE": "DEV002", "PKG_OUT_QTY": 504},
                {"DEVICE": "DEV-MOBILE-PKGOUT-B", "PKG_OUT_QTY": 420},
            ],
        ),
        case(
            4,
            "HBM제품의 WB공정에서 오늘 아침재공 제품별로 알려줘",
            "hbm_wb_boh_wip_by_product",
            [job("wip", "wip_data", "20260630", {"OPER_NAME": in_values(WB_PROCESSES)})],
            (
                "df = sources['wip_data']\n"
                "df = df[df['TSV_DIE_TYP'].fillna('').astype(str).ne('')]\n"
                "result = df.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['WIP'].sum().rename(columns={'WIP': 'BOH_WIP'}).sort_values(['BOH_WIP', 'DEVICE'], ascending=[False, True])"
            ),
            ["BOH_WIP", "DEVICE"],
            expected_row_count=2,
            expected_first_row={"DEVICE": "DEV-HBM", "BOH_WIP": 1305},
            expected_rows=[{"DEVICE": "DEV-HBM-B", "BOH_WIP": 315}],
        ),
        case(
            5,
            "6/27일 W/B공정에서 세부 공정별 생산실적과 아침재공 수량 알려줘",
            "wb_detail_production_and_boh_wip",
            [
                job("production", "production_data", "20260627", {"OPER_NAME": in_values(WB_PROCESSES)}),
                job("wip", "wip_data", "20260626", {"OPER_NAME": in_values(WB_PROCESSES)}),
            ],
            (
                "prod = sources['production_data'].groupby('OPER_NAME', as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})\n"
                "wip = sources['wip_data'].groupby('OPER_NAME', as_index=False)['WIP'].sum().rename(columns={'WIP': 'BOH_WIP'})\n"
                "result = prod.merge(wip, on='OPER_NAME', how='outer').fillna(0).sort_values('OPER_NAME')"
            ),
            ["OPER_NAME", "TOTAL_PRODUCTION", "BOH_WIP"],
            expected_row_count=6,
            expected_first_row={"OPER_NAME": "W/B1", "TOTAL_PRODUCTION": 0},
            expected_rows=[
                {"OPER_NAME": "W/B1", "TOTAL_PRODUCTION": 0},
                {"OPER_NAME": "W/B6", "BOH_WIP": 0},
            ],
        ),
        case(
            6,
            "HBM제품 FCB공정에서 오늘 아침재공 제품별로 알려줘",
            "hbm_fcb_boh_wip_by_product",
            [job("wip", "wip_data", "20260630", {"OPER_NAME": in_values(FCB_PROCESSES)})],
            (
                "df = sources['wip_data']\n"
                "df = df[df['TSV_DIE_TYP'].fillna('').astype(str).ne('')]\n"
                "result = df.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['WIP'].sum().rename(columns={'WIP': 'BOH_WIP'}).sort_values(['BOH_WIP', 'DEVICE'], ascending=[False, True])"
            ),
            ["BOH_WIP", "DEVICE"],
            expected_row_count=2,
            expected_first_row={"DEVICE": "DEV-HBM", "BOH_WIP": 801},
            expected_rows=[{"DEVICE": "DEV-HBM-B", "BOH_WIP": 225}],
        ),
        case(
            7,
            "6월 30일 FCB/H 공정 실적이 있는 Device 알려줘",
            "fcbh_device_with_production",
            [job("production", "production_data", "20260630", {"OPER_NAME": eq("FCB/H")})],
            "df = sources['production_data']\ndf = df[df['PRODUCTION'] > 0]\nresult = df.groupby('DEVICE', as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'}).sort_values(['TOTAL_PRODUCTION', 'DEVICE'], ascending=[False, True])",
            ["DEVICE", "TOTAL_PRODUCTION"],
            expected_row_count=4,
            expected_first_row={"DEVICE": "DEV-SP-DDR5", "TOTAL_PRODUCTION": 608},
            expected_rows=[{"DEVICE": "DEV-HBM", "TOTAL_PRODUCTION": 440}],
            forbidden_values={"DEVICE": ["DEV-FCBH-ZERO"]},
        ),
        product_case(
            8,
            "RG 32G DDR4 FBGA 96 DDP 제품 BG공정에서 생산량과 재공수량 알려줘",
            "rg_ddr4_bg_production_and_wip",
            "RG 32G DDR4 FBGA 96 DDP",
            [
                job("production_today", "production_data", "20260701", {"OPER_NAME": in_values(BG_PROCESSES)}),
                job("wip_today", "wip_data", "20260701", {"OPER_NAME": in_values(BG_PROCESSES)}),
            ],
            (
                "prod = match_product_tokens('RG 32G DDR4 FBGA 96 DDP', sources['production_data'])\n"
                "wip = match_product_tokens('RG 32G DDR4 FBGA 96 DDP', sources['wip_data'])\n"
                "prod = prod.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})\n"
                "wip = wip.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['WIP'].sum().rename(columns={'WIP': 'TOTAL_WIP'})\n"
                "result = prod.merge(wip, on=['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], how='outer').fillna(0)"
            ),
            ["TOTAL_PRODUCTION", "TOTAL_WIP", "DEVICE"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV-RG-DDR4", "TOTAL_PRODUCTION": 1117, "TOTAL_WIP": 827},
            forbidden_values={"DEVICE": ["DEV-RG-DECOY-LEAD78", "DEV-RG-DECOY-DEN16", "DEV-RG-DECOY-FCBGA"]},
        ),
        product_case(
            9,
            "FCB 공정에서 SP 16G DDR5 2ND X4 78 FCBGA SDP 제품의 전일 생산량 알려줘",
            "sp_ddr5_fcb_previous_day_production",
            "SP 16G DDR5 2ND X4 78 FCBGA SDP",
            [job("production", "production_data", "20260630", {"OPER_NAME": in_values(FCB_PROCESSES)})],
            (
                "df = match_product_tokens('SP 16G DDR5 2ND X4 78 FCBGA SDP', sources['production_data'])\n"
                "result = df.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})"
            ),
            ["TOTAL_PRODUCTION", "DEVICE"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV-SP-DDR5", "TOTAL_PRODUCTION": 1791},
            forbidden_values={"DEVICE": ["DEV-SP-DECOY-X8", "DEV-SP-DECOY-LEAD96", "DEV-SP-DECOY-VFBGA"]},
        ),
        case(
            10,
            "6/24일 투입 실적 대비 D/S1, DA1공정에서 WIP 많은 제품 알려줘",
            "input_vs_ds1_da1_wip_rank",
            [
                job("production", "input_data", "20260624", {"OPER_NAME": eq("INPUT")}),
                job("wip", "wip_data", "20260624", {"OPER_NAME": in_values(["D/S1", "D/A1"])}),
            ],
            (
                "keys = ['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE']\n"
                "inp = sources['input_data'].groupby(keys, as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'INPUT_QTY'})\n"
                "wip = sources['wip_data'].groupby(keys, as_index=False)['WIP'].sum().rename(columns={'WIP': 'TOTAL_WIP'})\n"
                "result = inp.merge(wip, on=keys, how='inner')\n"
                "result = result.sort_values(['TOTAL_WIP', 'DEVICE'], ascending=[False, True])"
            ),
            ["INPUT_QTY", "TOTAL_WIP", "DEVICE"],
            min_rows=6,
            expected_first_row={"DEVICE": "DEV-RANK-1", "INPUT_QTY": 100, "TOTAL_WIP": 1200},
            expected_rows=[{"DEVICE": "DEV-RANK-6", "INPUT_QTY": 100, "TOTAL_WIP": 200}],
        ),
        case(
            11,
            "오늘 현시간 기준 INPUT실적은 있으나 D/A공정 WIP 없는 제품 확인해줘",
            "input_exists_no_da_wip",
            [
                job("production_today", "input_data", "20260701", {"OPER_NAME": eq("INPUT")}),
                job("wip_today", "da_wip_data", "20260701", {"OPER_NAME": in_values(DA_PROCESSES)}),
            ],
            (
                "keys = ['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE']\n"
                "inp = sources['input_data'].groupby(keys, as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'INPUT_QTY'})\n"
                "da = sources['da_wip_data'].groupby(keys, as_index=False)['WIP'].sum().rename(columns={'WIP': 'DA_WIP'})\n"
                "inp = inp[inp['INPUT_QTY'] > 0]\n"
                "da = da[da['DA_WIP'] > 0]\n"
                "merged = inp.merge(da[keys], on=keys, how='left', indicator=True)\n"
                "result = merged[merged['_merge'].eq('left_only')].drop(columns=['_merge']).sort_values('INPUT_QTY', ascending=False)\n"
                "result['DA_WIP'] = 0"
            ),
            ["INPUT_QTY", "DA_WIP", "DEVICE"],
            expected_row_count=6,
            expected_first_row={"DEVICE": "DEV-L218K8H", "INPUT_QTY": 440, "DA_WIP": 0},
            expected_rows=[{"DEVICE": "DEV-ZERO-DA-WIP", "INPUT_QTY": 50, "DA_WIP": 0}],
            forbidden_values={"DEVICE": ["DEV-ZERO-INPUT"]},
        ),
        case(
            12,
            "FCB 공정 생산 실적과 W/B2 공정 재공수량을 제품별로 비교해줘",
            "source_specific_fcb_production_vs_wb2_wip",
            [
                job("production_today", "production_data", "20260701", {"OPER_NAME": in_values(FCB_PROCESSES)}),
                job("wip_today", "wip_data", "20260701", {"OPER_NAME": eq("W/B2")}),
            ],
            (
                "keys = ['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE']\n"
                "prod = sources['production_data'].groupby(keys, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'FCB_PRODUCTION'})\n"
                "wip = sources['wip_data'].groupby(keys, dropna=False, as_index=False)['WIP'].sum().rename(columns={'WIP': 'WB2_WIP'})\n"
                "result = prod.merge(wip, on=keys, how='outer')\n"
                "result['FCB_PRODUCTION'] = result['FCB_PRODUCTION'].fillna(0)\n"
                "result['WB2_WIP'] = result['WB2_WIP'].fillna(0)\n"
                "result = result.sort_values(['FCB_PRODUCTION', 'WB2_WIP'], ascending=[False, False])"
            ),
            ["FCB_PRODUCTION", "WB2_WIP", "DEVICE"],
            expected_row_count=4,
            expected_rows=[
                {"DEVICE": "DEV-SP-DDR5", "WB2_WIP": 0},
                {"DEVICE": "DEV001", "FCB_PRODUCTION": 692, "WB2_WIP": 135},
            ],
        ),
        case(
            13,
            "W/B공정 IN TAT 10시간이상 된 LOT 알려줘",
            "wb_lot_in_tat_ge_10",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values(WB_PROCESSES), "IN_TAT": {"operator": "ge", "value": 10}})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'IN_TAT', 'HOLD_STAT']].sort_values(['IN_TAT', 'LOT_ID'], ascending=[False, True])"
            ),
            ["LOT_ID", "OPER_NAME", "IN_TAT"],
            expected_row_count=2,
            expected_first_row={"LOT_ID": "T1234567GEN1", "OPER_NAME": "W/B1", "IN_TAT": 12.5},
            expected_rows=[{"LOT_ID": "V-WB3-HIGH-TAT", "IN_TAT": 11.0}],
            forbidden_values={"LOT_ID": ["V-WB2-LOW-TAT"]},
        ),
        ordered_range_case(
            14,
            "D/S1~D/A4 공정 Hold 된 Lot ID 알려줘",
            "ordered_process_range_hold_lots",
            "D/S1~D/A4",
            "lot_data",
            [job("lot_status", "lot_data")],
            (
                "df = filter_ordered_range('D/S1~D/A4', sources['lot_data'])\n"
                "df = df[df['HOLD_STAT'].eq('OnHold')]\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'HOLD_STAT', 'HOLD_REASON']].sort_values(['OPER_NAME', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
            expected_row_count=2,
            expected_rows=[
                {"LOT_ID": "V-RANGE-START-HOLD", "OPER_NAME": "D/S1"},
                {"LOT_ID": "V-RANGE-MIDDLE-HOLD", "OPER_NAME": "D/A5"},
            ],
        ),
        ordered_range_case(
            15,
            "7월 1일 D/A1~W/B6 공정 구간의 공정별 생산량을 OPER_SEQ 순서로 알려줘",
            "ordered_process_range_production",
            "D/A1~W/B6",
            "production_data",
            [job("production_today", "production_data", "20260701")],
            (
                "df = filter_ordered_range('D/A1~W/B6', sources['production_data'])\n"
                "df['OPER_SEQ_NUM'] = pd.to_numeric(df['OPER_SEQ'], errors='coerce')\n"
                "result = df.groupby(['OPER_SEQ_NUM', 'OPER_NAME'], as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})\n"
                "result = result.sort_values(['OPER_SEQ_NUM', 'OPER_NAME']).rename(columns={'OPER_SEQ_NUM': 'OPER_SEQ'})"
            ),
            ["OPER_SEQ", "OPER_NAME", "TOTAL_PRODUCTION"],
            expected_row_count=13,
            expected_first_row={"OPER_SEQ": 100, "OPER_NAME": "D/A1", "TOTAL_PRODUCTION": 11212},
            expected_rows=[{"OPER_SEQ": 250, "OPER_NAME": "W/B6"}],
            forbidden_values={"OPER_NAME": ["W/BM", "FCB1"]},
        ),
        case(
            16,
            "DA, WB공정 HOLD LOT 알려줘",
            "da_wb_group_hold_lots_comma_shared_suffix",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values([*DA_PROCESSES, *WB_PROCESSES]), "HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'HOLD_REASON']].sort_values(['OPER_NAME', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "HOLD_REASON"],
            expected_row_count=2,
            expected_rows=[
                {"LOT_ID": "T1234567GEN1", "OPER_NAME": "W/B1"},
                {"LOT_ID": "V-RANGE-MIDDLE-HOLD", "OPER_NAME": "D/A5"},
            ],
            forbidden_values={"LOT_ID": ["V-RANGE-START-HOLD"]},
        ),
        case(
            17,
            "WB & DA 공정 Hold Lot LIST알려줘",
            "wb_da_group_hold_lots_ampersand",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values([*WB_PROCESSES, *DA_PROCESSES]), "HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'HOLD_REASON']].sort_values(['OPER_NAME', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "HOLD_REASON"],
            expected_row_count=2,
            expected_rows=[
                {"LOT_ID": "T1234567GEN1", "OPER_NAME": "W/B1"},
                {"LOT_ID": "V-RANGE-MIDDLE-HOLD", "OPER_NAME": "D/A5"},
            ],
        ),
        case(
            18,
            "D/S1&D/A 공정 Hold Lot LIST알려줘",
            "single_process_and_da_group_hold_lots",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values(["D/S1", *DA_PROCESSES]), "HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'HOLD_REASON']].sort_values(['OPER_NAME', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "HOLD_REASON"],
            expected_row_count=2,
            expected_rows=[
                {"LOT_ID": "V-RANGE-START-HOLD", "OPER_NAME": "D/S1"},
                {"LOT_ID": "V-RANGE-MIDDLE-HOLD", "OPER_NAME": "D/A5"},
            ],
            forbidden_values={"LOT_ID": ["T1234567GEN1"]},
        ),
        case(
            19,
            "7월 5일 FCB1,FCB2,FCB/H 공정 실적 알려줘",
            "explicit_fcb_process_list_production",
            [job("production", "production_data", "20260705", {"OPER_NAME": in_values(FCB_PROCESSES)})],
            (
                "df = sources['production_data'].copy()\n"
                "result = df.groupby('OPER_NAME', as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'}).sort_values('OPER_NAME')"
            ),
            ["OPER_NAME", "TOTAL_PRODUCTION"],
            expected_row_count=3,
            expected_rows=[
                {"OPER_NAME": "FCB1"},
                {"OPER_NAME": "FCB2"},
                {"OPER_NAME": "FCB/H"},
            ],
        ),
        case(
            20,
            "7/9 D/A1, D/A2공정에서 생산 실적 알려줘",
            "explicit_da1_da2_production",
            [job("production", "production_data", "20260709", {"OPER_NAME": in_values(["D/A1", "D/A2"])})],
            (
                "df = sources['production_data'].copy()\n"
                "result = df.groupby('OPER_NAME', as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'}).sort_values('OPER_NAME')"
            ),
            ["OPER_NAME", "TOTAL_PRODUCTION"],
            expected_row_count=2,
            expected_rows=[
                {"OPER_NAME": "D/A1"},
                {"OPER_NAME": "D/A2"},
            ],
        ),
        case(
            21,
            "오늘 WBM 공정의 제품별 생산량을 알려줘. 제품 정보가 비어 있는 행도 제외하지 말고, 비어 있는 제품 정보는 빈칸으로, 생산량이 비어 있으면 0으로 보여줘.",
            "wbm_product_production_with_blank_dimensions",
            [job("production_today", "production_data", "20260701", {"OPER_NAME": eq("W/BM")})],
            (
                f"dims = {PRODUCT_KEYS!r}\n"
                "df = sources['production_data'].copy()\n"
                "df['PRODUCTION'] = pd.to_numeric(df['PRODUCTION'], errors='coerce').fillna(0)\n"
                "result = df.groupby(dims, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})\n"
                "for column in dims:\n"
                "    result[column] = result[column].fillna('').replace(r'^\\s*$', '', regex=True)\n"
                "result = result.sort_values(['TOTAL_PRODUCTION', 'DEVICE'], ascending=[False, True])"
            ),
            [*PRODUCT_KEYS, "TOTAL_PRODUCTION"],
            expected_row_count=4,
            expected_first_row={"DEVICE": "DEV-WBM-B-SHIFT-DECOY", "TOTAL_PRODUCTION": 900},
            expected_rows=[
                {"DEVICE": "DEV-WBM-BLANK", "TECH": "", "DEN": "", "MODE": "", "TOTAL_PRODUCTION": 37},
                {"DEVICE": "DEV-WBM-NULL-QTY", "TOTAL_PRODUCTION": 0},
            ],
        ),
        case(
            22,
            "현재 제품 중 TECH, DEN, PKG_TYPE2, MCP_NO는 같지만 MODE, PKG_TYPE1 또는 LEAD가 다른 제품들을 찾아서 보여줘.",
            "compare_product_attributes_with_same_base_keys",
            [job("production_today", "product_data", "20260701", {"TECH": eq("CMP")})],
            (
                "df = sources['product_data'].copy()\n"
                "group_cols = ['TECH', 'DEN', 'PKG_TYPE2', 'MCP_NO']\n"
                "comp_cols = ['MODE', 'PKG_TYPE1', 'LEAD']\n"
                "unique_rows = df[group_cols + comp_cols + ['DEVICE']].drop_duplicates()\n"
                "counts = unique_rows.groupby(group_cols, dropna=False)[comp_cols].nunique(dropna=False)\n"
                "valid_keys = counts[(counts > 1).any(axis=1)].reset_index()[group_cols]\n"
                "result = unique_rows.merge(valid_keys, on=group_cols, how='inner').sort_values(['MODE', 'PKG_TYPE1', 'LEAD', 'DEVICE'])"
            ),
            ["TECH", "DEN", "PKG_TYPE2", "MCP_NO", "MODE", "PKG_TYPE1", "LEAD", "DEVICE"],
            expected_row_count=4,
            expected_rows=[
                {"DEVICE": "DEV-COMPARE-BASE", "MCP_NO": ""},
                {"DEVICE": "DEV-COMPARE-MODE", "MODE": "DDR5"},
                {"DEVICE": "DEV-COMPARE-PKG1", "PKG_TYPE1": "VFBGA"},
                {"DEVICE": "DEV-COMPARE-LEAD", "LEAD": "78"},
            ],
            forbidden_values={"DEVICE": ["DEV-COMPARE-MCP_DECOY"]},
        ),
        case(
            23,
            "FCB2공정 제품별 UPH 알려줘",
            "fcb2_product_uph_detail",
            [job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("FCB2")})],
            (
                "df = sources['uph_data'].copy()\n"
                "group_cols = ['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'EQP_MODEL', 'RECIPE_ID', 'OPER_NAME']\n"
                "result = df.groupby(group_cols, dropna=False, as_index=False)['UPH'].mean().sort_values(['UPH', 'RECIPE_ID'], ascending=[False, True])"
            ),
            ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
            expected_row_count=2,
            expected_first_row={"RECIPE_ID": "RCP-FCB2-B", "UPH": 173.4},
            expected_rows=[{"RECIPE_ID": "RCP-FCB2-A", "UPH": 140.0}],
        ),
        product_case(
            24,
            "WB공정 L-217제품 차수별, 장비 기종별 UPH 알려줘",
            "l217_wb_uph_by_step_and_model",
            "L-217",
            [job("eqp_uph", "uph_data", filters={"OPER_NAME": in_values(WB_PROCESSES)})],
            (
                "df = match_product_tokens('L-217', sources['uph_data'])\n"
                "result = df.groupby(['OPER_NAME', 'EQP_MODEL'], dropna=False, as_index=False)['UPH'].mean().round({'UPH': 2}).sort_values(['OPER_NAME', 'EQP_MODEL'])"
            ),
            ["OPER_NAME", "EQP_MODEL", "UPH"],
            expected_row_count=2,
            expected_rows=[
                {"OPER_NAME": "W/B1", "EQP_MODEL": "EQM-A", "UPH": 123.4},
                {"OPER_NAME": "W/B2", "EQP_MODEL": "EQM-BG", "UPH": 97.5},
            ],
        ),
        product_case(
            25,
            "F315 L-116로 시작하는 제품 WB 공정 차수별 UPH 알려줘",
            "f315_l116_wb_uph_by_step",
            "F315 L-116",
            [job("eqp_uph", "uph_data", filters={"OPER_NAME": in_values(WB_PROCESSES)})],
            (
                "df = match_product_tokens('F315 L-116', sources['uph_data'])\n"
                "result = df.groupby('OPER_NAME', dropna=False, as_index=False)['UPH'].mean().round({'UPH': 2}).sort_values('OPER_NAME')"
            ),
            ["OPER_NAME", "UPH"],
            expected_row_count=1,
            expected_first_row={"OPER_NAME": "W/B1", "UPH": 112.0},
        ),
        case(
            26,
            "현재 D/A1 공정의 장비 모델, Recipe, 공정, UPH를 보여줘",
            "da1_uph_default_detail_columns",
            [job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("D/A1")})],
            (
                "df = sources['uph_data'].copy()\n"
                "result = df[['EQP_MODEL', 'RECIPE_ID', 'OPER_NAME', 'UPH']].sort_values(['EQP_MODEL', 'RECIPE_ID'])"
            ),
            ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
            expected_row_count=1,
            expected_first_row={"EQP_MODEL": "EQM-HBM", "RECIPE_ID": "RCP-002", "OPER_NAME": "D/A1", "UPH": 88.2},
        ),
        case(
            27,
            "현재 D/A1 공정에 배정된 장비를 장비 모델과 Recipe 조합별로 보여줘",
            "da1_equipment_by_model_and_recipe",
            [job("equipment_assign", "equipment_data", filters={"OPER_NAME": eq("D/A1")})],
            (
                "df = sources['equipment_data'].copy()\n"
                "result = df[['EQP_ID', 'EQP_MODEL', 'RECIPE_ID', 'OPER_NAME']].drop_duplicates().sort_values(['EQP_MODEL', 'RECIPE_ID', 'EQP_ID'])"
            ),
            ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            expected_row_count=1,
            expected_first_row={"EQP_ID": "EQP002", "EQP_MODEL": "EQM-HBM", "RECIPE_ID": "RCP-002", "OPER_NAME": "D/A1"},
        ),
        case(
            28,
            "W/B공정 현재 HOLD LOT와 HOLD사유 알려줘",
            "wb_current_hold_lots_with_reason",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values(WB_PROCESSES), "HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'OPER_NAME', 'HOLD_STAT', 'HOLD_REASON', 'OPER_IN_TM']].sort_values(['OPER_IN_TM', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON", "OPER_IN_TM"],
            expected_row_count=1,
            expected_first_row={"LOT_ID": "T1234567GEN1", "OPER_NAME": "W/B1", "HOLD_REASON": "검증용 HOLD"},
        ),
        case(
            29,
            "현재 HOLD 중인 LOT 목록과 LOT별 UNIT 수량, Wafer 수량, 현재·누적 TAT를 보여줘",
            "current_hold_lot_details",
            [job("lot_status", "lot_data", filters={"HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'DEVICE', 'PROD_QTY', 'WF_QTY', 'IN_TAT', 'CUM_TAT', 'HOLD_STAT', 'HOLD_REASON']].rename(columns={'PROD_QTY': 'UNIT_QTY', 'WF_QTY': 'WAFER_QTY'}).sort_values('LOT_ID')"
            ),
            ["LOT_ID", "UNIT_QTY", "WAFER_QTY", "IN_TAT", "CUM_TAT"],
            expected_row_count=3,
            expected_rows=[
                {"LOT_ID": "T1234567GEN1", "UNIT_QTY": 100, "WAFER_QTY": 25},
                {"LOT_ID": "V-RANGE-MIDDLE-HOLD", "UNIT_QTY": 35, "WAFER_QTY": 9},
                {"LOT_ID": "V-RANGE-START-HOLD", "UNIT_QTY": 38, "WAFER_QTY": 9},
            ],
        ),
        case(
            30,
            "오늘 DA공정에서 생산량 상위 3개 제품과 각 제품에 할당된 장비 대수 및 LIST를 알려줘",
            "top3_da_products_with_equipment_count_and_list",
            [
                job("production_today", "production_data", "20260701", {"OPER_NAME": in_values(DA_PROCESSES)}),
                job("equipment_assign", "equipment_data"),
            ],
            (
                "keys = ['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO']\n"
                "prod = sources['production_data'].copy()\n"
                "eqp = sources['equipment_data'].copy()\n"
                "for column in keys:\n"
                "    prod[column] = prod[column].astype('string').fillna('').str.strip().replace({'<NA>': '', 'empty': '', 'nan': '', 'None': '', 'null': ''})\n"
                "    eqp[column] = eqp[column].astype('string').fillna('').str.strip().replace({'<NA>': '', 'empty': '', 'nan': '', 'None': '', 'null': ''})\n"
                "prod_agg = prod.groupby(keys, dropna=False, as_index=False)['PRODUCTION'].sum().sort_values('PRODUCTION', ascending=False).head(3)\n"
                "eqp_agg = eqp.groupby(keys, dropna=False, as_index=False).agg(EQUIP_COUNT=('EQP_ID', 'nunique'), EQUIP_LIST=('EQP_ID', lambda values: ', '.join(sorted(values.dropna().astype(str).unique().tolist()))))\n"
                "result = prod_agg.merge(eqp_agg, on=keys, how='left')\n"
                "result['EQUIP_COUNT'] = result['EQUIP_COUNT'].fillna(0)\n"
                "result['EQUIP_LIST'] = result['EQUIP_LIST'].fillna('')\n"
                "result = result.sort_values('PRODUCTION', ascending=False)"
            ),
            [*TARGET_PRODUCT_KEYS, "PRODUCTION", "EQUIP_COUNT", "EQUIP_LIST"],
            expected_row_count=3,
            expected_first_row={
                "TECH": "VM",
                "MCP_NO": "",
                "PRODUCTION": 10000,
                "EQUIP_COUNT": 2,
                "EQUIP_LIST": "EQP-NULL-1, EQP-NULL-2",
            },
        ),
    ]


def representative_cases() -> list[dict[str, Any]]:
    """Return 22 active operational questions plus six unrelated legacy checks.

    The fixture plans are intentionally explicit test inputs.  They verify that
    the dummy sources can answer each representative question; they do not
    become production routing or Domain rules.

    Operational IDs remain stable.  Questions that need an unavailable source
    stay defined below for audit history but are filtered from active runs via
    ``EXCLUDED_OPERATIONAL_CASE_IDS``.
    """

    legacy_by_id = {int(item["id"]): item for item in _legacy_representative_cases()}
    retained: list[dict[str, Any]] = []
    for new_id, legacy_id in ((26, 7), (27, 10), (28, 11), (29, 12), (30, 21), (31, 22)):
        item = deepcopy(legacy_by_id[legacy_id])
        item["id"] = new_id
        retained.append(item)
    # The first retained question is also used as a multi-turn fixture, so its
    # original wording stays unchanged.  Correct the two display-only labels
    # that were historically written without the D/A slash.
    retained[1]["question"] = "6/24일 투입 실적 대비 D/S1, D/A1공정에서 WIP 많은 제품 알려줘"
    retained[2]["question"] = "현시간 기준 INPUT실적은 있으나 D/A공정 WIP 없는 제품 확인해줘"

    operational = [
        case(
            1,
            "FCB공정 R0429 RECIPE의 UPH알려줘",
            "fcb_recipe_uph",
            [
                job(
                    "eqp_uph",
                    "uph_data",
                    filters={
                        "OPER_NAME": in_values(FCB_PROCESSES),
                        "RECIPE_ID": {"operator": "starts_with", "value": "R0429"},
                    },
                )
            ],
            "df = sources['uph_data'].copy()\nresult = df[['OPER_NAME', 'RECIPE_ID', 'UPH']].sort_values(['OPER_NAME', 'RECIPE_ID'])",
            ["OPER_NAME", "RECIPE_ID", "UPH"],
            expected_row_count=3,
            expected_first_row={"OPER_NAME": "FCB1", "RECIPE_ID": "R0429", "UPH": 154.2},
        ),
        case(
            2,
            "M/D공정 LEAD별 장비 ASSIGN 댓수와 UPH알려줘",
            "md_equipment_assign_count_and_uph_by_lead",
            [
                job("equipment_assign", "assign_data", filters={"OPER_NAME": eq("M/D")}),
                job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("M/D")}),
            ],
            (
                "join_keys = ['EQP_MODEL', 'RECIPE_ID', 'OPER_NAME']\n"
                "assign = sources['assign_data'].copy()\n"
                "uph = sources['uph_data'][join_keys + ['UPH']].copy()\n"
                "merged = assign.merge(uph, on=join_keys, how='left', validate='one_to_one')\n"
                "result = merged.groupby('LEAD', as_index=False).agg(ASSIGN_COUNT=('EQP_ID', 'nunique'), AVG_UPH=('UPH', 'mean')).sort_values('LEAD')"
            ),
            ["LEAD", "ASSIGN_COUNT", "AVG_UPH"],
            expected_rows=[{"LEAD": "78", "ASSIGN_COUNT": 2}, {"LEAD": "267", "ASSIGN_COUNT": 1}],
        ),
        case(
            3,
            "SBM공정 제품별 보유Capa 알려줘",
            "sbm_available_capacity_by_product",
            [
                job("equipment_assign", "assign_data", filters={"OPER_NAME": eq("SBM")}),
                job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("SBM")}),
            ],
            _capacity_by_product_code("assign_data", "uph_data"),
            [*TARGET_PRODUCT_KEYS, "EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
            min_rows=2,
            expected_rows=[{"LEAD": "200", "EQP_COUNT": 1, "AVAILABLE_CAPA": 3024.0}],
        ),
        case(
            4,
            "D/A공정 78LEAD 제품별 장비현황과 CAPA 알려줘",
            "da_lead_78_equipment_status_and_capacity",
            [
                job("equipment_assign", "assign_data", filters={"OPER_NAME": in_values(DA_PROCESSES), "LEAD": eq("78")}),
                job("eqp_uph", "uph_data", filters={"OPER_NAME": in_values(DA_PROCESSES), "LEAD": eq("78")}),
            ],
            _capacity_by_equipment_code("assign_data", "uph_data"),
            ["OPER_NAME", "EQP_ID", "EQP_MODEL", "RECIPE_ID", "LEAD", "UPH", "AVAILABLE_CAPA"],
            min_rows=2,
            expected_rows=[{"EQP_ID": "EQP-DA78-A", "LEAD": "78", "AVAILABLE_CAPA": 3312.0}],
        ),
        case(
            5,
            "현재 SBM공정 Down장비들 Event 알려줘",
            "sbm_current_down_equipment_events",
            [job("eqp_down_list", "down_data", "20260701", {"OPER_NAME": eq("SBM"), "EQP_STAT_CD": eq("DOWN")})],
            "df = sources['down_data'].copy()\nresult = df[['EQP_ID', 'OPER_NAME', 'DOWN_TM', 'LAST_EVENT_DESC', 'HIST_DESC']].sort_values('EQP_ID')",
            ["EQP_ID", "OPER_NAME", "DOWN_TM", "LAST_EVENT_DESC", "HIST_DESC"],
            expected_row_count=1,
            expected_first_row={"EQP_ID": "EQP-SBM-DOWN-01", "OPER_NAME": "SBM"},
        ),
        case(
            6,
            "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
            "sbm_lead_266_lot_list",
            [job("lot_status", "lot_data", filters={"OPER_NAME": eq("SBM"), "LEAD": eq("266")})],
            "df = sources['lot_data'].copy()\nresult = df[['LOT_ID', 'OPER_NAME', 'LEAD', 'MCP_NO', 'LOT_STAT']].drop_duplicates().sort_values('LOT_ID')",
            ["LOT_ID", "OPER_NAME", "LEAD", "MCP_NO", "LOT_STAT"],
            expected_row_count=2,
            expected_rows=[{"LOT_ID": "SBM-266-LOT-01"}, {"LOT_ID": "SBM-266-LOT-02"}],
        ),
        case(
            7,
            "WSD 공정 L-085 제품 몇 Lot 보유 하고 있는지 알려줘",
            "wsd_l085_distinct_lot_count",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values(["WSD1", "WSD2"]), "MCP_NO": {"operator": "contains", "value": "L-085"}})],
            "df = sources['lot_data'].copy()\nresult = df.groupby('MCP_NO', as_index=False).agg(LOT_COUNT=('LOT_ID', 'nunique')).sort_values('MCP_NO')",
            ["MCP_NO", "LOT_COUNT"],
            expected_row_count=1,
            expected_first_row={"MCP_NO": "L-085A1", "LOT_COUNT": 2},
        ),
        case(
            8,
            "16G SP FCBGA SDP 78 제품 S/G 공정 UPH 확인 해줘",
            "sg_sp_16g_fcbga_sdp_78_uph",
            [job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("S/G")})],
            (
                "df = sources['uph_data'].copy()\n"
                "df = df[(df['TECH'].eq('SP')) & (df['DEN'].eq('16G')) & (df['PKG_TYPE1'].eq('FCBGA')) & (df['PKG_TYPE2'].eq('SDP')) & (df['LEAD'].astype(str).eq('78'))]\n"
                "result = df[['OPER_NAME', 'EQP_MODEL', 'RECIPE_ID', 'UPH']].sort_values('RECIPE_ID')"
            ),
            ["OPER_NAME", "EQP_MODEL", "RECIPE_ID", "UPH"],
            expected_row_count=1,
            expected_first_row={"OPER_NAME": "S/G", "RECIPE_ID": "RCP-SG-78-A", "UPH": 151.0},
        ),
        case(
            9,
            "8월19일 제품별 out계획 알려줘. 단, out계획이 0인 제품은 제외 해줘",
            "august_19_product_out_plan_excluding_zero",
            [job("target", "target_data", "2026-08-19")],
            (
                "df = sources['target_data'].copy()\n"
                "df = df[df['DATE'].astype(str).eq('2026-08-19')].copy()\n"
                "df['OUT_PLAN'] = pd.to_numeric(df['OUT_PLAN'], errors='coerce').fillna(0)\n"
                "result = df[df['OUT_PLAN'].gt(0)][['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'OUT_PLAN']].sort_values('OUT_PLAN', ascending=False)"
            ),
            [*TARGET_PRODUCT_KEYS, "OUT_PLAN"],
            min_rows=1,
            forbidden_values={"MCP_NO": ["OUT-ZERO"]},
        ),
        ordered_range_case(
            10,
            "D/A1~W/B6 공정 IN TAT 1일 이상된 LOT LIST 알려줘",
            "ordered_process_range_lot_in_tat_one_day",
            "D/A1~W/B6",
            "lot_data",
            [job("lot_status", "lot_data")],
            (
                "df = filter_ordered_range('D/A1~W/B6', sources['lot_data']).copy()\n"
                "df['IN_TAT'] = pd.to_numeric(df['IN_TAT'], errors='coerce')\n"
                "result = df[df['IN_TAT'].ge(24)][['LOT_ID', 'OPER_NAME', 'IN_TAT']].sort_values(['OPER_NAME', 'LOT_ID'])"
            ),
            ["LOT_ID", "OPER_NAME", "IN_TAT"],
            expected_rows=[{"LOT_ID": "RANGE-DA1-24H", "IN_TAT": 24.0}, {"LOT_ID": "RANGE-WB6-36H", "IN_TAT": 36.0}],
            forbidden_values={"LOT_ID": ["RANGE-WB4-UNDER-24H"]},
        ),
        case(
            11,
            "DA, PCO, DI 공정 HOLD LOT 알려줘",
            "da_pco_di_hold_lots",
            [job("lot_status", "lot_data", filters={"OPER_NAME": in_values([*DA_PROCESSES, "PCO1", "D/I"]), "HOLD_STAT": eq("OnHold")})],
            "df = sources['lot_data'].copy()\nresult = df[['LOT_ID', 'OPER_NAME', 'HOLD_REASON']].sort_values(['OPER_NAME', 'LOT_ID'])",
            ["LOT_ID", "OPER_NAME", "HOLD_REASON"],
            expected_rows=[{"LOT_ID": "HOLD-DA1-01"}, {"LOT_ID": "HOLD-PCO1-01"}, {"LOT_ID": "HOLD-DI-01"}],
        ),
        case(
            12,
            "어제 L-267제품 OUT계획, PKG OUT 실적 알려줘",
            "yesterday_l267_out_plan_and_pkg_out_actual",
            [
                job("target", "target_data", "2026-06-30"),
                # L-267은 등록된 product-token helper가 source별로 적용하므로
                # retrieval filter 계약에는 PKG OUT 공정 조건만 요구한다.
                job("production", "pkg_out_data", "20260630", {"OPER_NAME": eq("PKG OUT")}),
            ],
            _target_and_production_code("target_data", "pkg_out_data", target_date="2026-06-30", mcp_no="L-267A1"),
            [*TARGET_PRODUCT_KEYS, "OUT_PLAN", "PKG_OUT_QTY"],
            expected_rows=[{"MCP_NO": "L-267A1", "PKG_OUT_QTY": 735}],
        ),
        case(
            13,
            "어제 제품별 OUT 계획과 PKG OUT",
            "yesterday_product_out_plan_and_pkg_out_actual",
            [
                job("target", "target_data", "2026-06-30"),
                job("production", "pkg_out_data", "20260630", {"OPER_NAME": eq("PKG OUT")}),
            ],
            _target_and_production_code("target_data", "pkg_out_data", target_date="2026-06-30"),
            [*TARGET_PRODUCT_KEYS, "OUT_PLAN", "PKG_OUT_QTY"],
            min_rows=1,
            expected_rows=[{"MCP_NO": "L-267A1", "PKG_OUT_QTY": 735}],
        ),
        case(
            14,
            "TSSJQ07AH LOT Hold 이력 조회해줘",
            "hold_history_by_lot_id",
            [job("hold_history", "history_data", required_params={"LOT_ID": "TSSJQ07AH"})],
            "df = sources['history_data'].copy()\nresult = df[['LOT_ID', 'OPER_NAME', 'HOLD_TM', 'HOLD_CD', 'HOLD_DESC']].sort_values('HOLD_TM')",
            ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
            expected_row_count=2,
            expected_rows=[{"HOLD_CD": "H-TSS-001"}, {"HOLD_CD": "H-TSS-002"}],
        ),
        case(
            15,
            "FCB공정 오늘 아침재공 제품별로 알려줘",
            "fcb_product_boh_wip",
            [job("wip", "wip_data", "20260630", {"OPER_NAME": in_values(FCB_PROCESSES)})],
            code_group_sum("wip_data", PRODUCT_KEYS, "WIP", "BOH_WIP"),
            [*PRODUCT_KEYS, "BOH_WIP"],
            min_rows=1,
        ),
        case(
            16,
            "오늘 PKG 투입계획 있는 제품들에 대해 INPUT실적 알려줘",
            "today_planned_products_input_actual",
            [
                job("target", "target_data", "2026-07-01"),
                job("production_today", "input_data", "20260701", {"OPER_NAME": eq("INPUT")}),
            ],
            _target_and_input_code("target_data", "input_data", target_date="2026-07-01"),
            [*TARGET_PRODUCT_KEYS, "INPUT_PLAN", "INPUT_QTY"],
            min_rows=1,
        ),
        case(
            17,
            "W/B공정 차수별 오늘 아침 재공 알려줘",
            "wb_stage_today_boh_wip",
            [job("wip", "wip_data", "20260630", {"OPER_NAME": in_values(WB_PROCESSES)})],
            "df = sources['wip_data'].copy()\nresult = df.groupby('OPER_NAME', as_index=False)['WIP'].sum().rename(columns={'WIP': 'BOH_WIP'}).sort_values('OPER_NAME')",
            ["OPER_NAME", "BOH_WIP"],
            min_rows=6,
        ),
        case(
            18,
            "S/G공정 제품별 오늘 아침 재공 알려줘",
            "sg_product_today_boh_wip",
            [job("wip", "wip_data", "20260630", {"OPER_NAME": eq("S/G")})],
            code_group_sum("wip_data", PRODUCT_KEYS, "WIP", "BOH_WIP"),
            [*PRODUCT_KEYS, "BOH_WIP"],
            expected_rows=[{"DEVICE": "DEV-SP-SG-78", "BOH_WIP": 88}],
        ),
        case(
            19,
            "W/B공정 차수별 현재 생산실적과 재공현황 알려줘",
            "wb_stage_current_production_and_wip",
            [
                job("production_today", "production_data", "20260701", {"OPER_NAME": in_values(WB_PROCESSES)}),
                job("wip_today", "wip_data", "20260701", {"OPER_NAME": in_values(WB_PROCESSES)}),
            ],
            _production_and_wip_by_grain_code("production_data", "wip_data", ["OPER_NAME"]),
            ["OPER_NAME", "PRODUCTION_QTY", "WIP_QTY"],
            min_rows=6,
        ),
        case(
            20,
            "S/G공정 제품별 현재 생산실적과 재공현황 알려줘",
            "sg_product_current_production_and_wip",
            [
                job("production_today", "production_data", "20260701", {"OPER_NAME": eq("S/G")}),
                job("wip_today", "wip_data", "20260701", {"OPER_NAME": eq("S/G")}),
            ],
            _production_and_wip_by_grain_code("production_data", "wip_data", PRODUCT_KEYS),
            [*PRODUCT_KEYS, "PRODUCTION_QTY", "WIP_QTY"],
            expected_rows=[{"DEVICE": "DEV-SP-SG-78", "PRODUCTION_QTY": 468, "WIP_QTY": 96}],
        ),
        case(
            21,
            "SBM공정 제품별 어제 아침재공과 생산실적 그리고 오늘 아침재공 알려줘",
            "sbm_product_yesterday_boh_production_today_boh",
            [
                job("wip", "boh_yesterday_data", "20260629", {"OPER_NAME": eq("SBM")}),
                job("production", "production_yesterday_data", "20260630", {"OPER_NAME": eq("SBM")}),
                job("wip", "boh_today_data", "20260630", {"OPER_NAME": eq("SBM")}),
            ],
            _three_way_boh_production_code("boh_yesterday_data", "production_yesterday_data", "boh_today_data"),
            [*TARGET_PRODUCT_KEYS, "BOH_YESTERDAY", "PRODUCTION_YESTERDAY", "BOH_TODAY"],
            min_rows=1,
        ),
        product_case(
            22,
            "DA공정 L-217제품 Assign 장비별 현재 가동율현황 조회해줘",
            "da_l217_assigned_equipment_utilization",
            "L-217",
            [
                job("equipment_assign", "assign_data", filters={"OPER_NAME": in_values(DA_PROCESSES)}),
                job("operation_rate_today", "operation_rate_data", "20260701"),
            ],
            (
                "assign = match_product_tokens('L-217', sources['assign_data']).copy()\n"
                "rate = sources['operation_rate_data'][['EQP_ID', 'EQP_EFFIC_CD', 'TOTAL_INTERVAL_RATE']].copy()\n"
                "result = assign.merge(rate, on='EQP_ID', how='left', validate='many_to_one')[['EQP_ID', 'EQP_MODEL', 'RECIPE_ID', 'OPER_NAME', 'MCP_NO', 'EQP_EFFIC_CD', 'TOTAL_INTERVAL_RATE']].sort_values('EQP_ID')"
            ),
            ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME", "MCP_NO", "EQP_EFFIC_CD", "TOTAL_INTERVAL_RATE"],
            expected_rows=[{"EQP_ID": "EQP-DA217-A", "TOTAL_INTERVAL_RATE": 86.5}],
        ),
        case(
            23,
            "D/A공정 장비들 현재 작업제품들과 가동율현황 조회해줘",
            "da_current_equipment_product_and_utilization",
            [
                job("equipment_assign", "assign_data", filters={"OPER_NAME": in_values(DA_PROCESSES)}),
                job("operation_rate_today", "operation_rate_data", "20260701"),
            ],
            (
                "assign = sources['assign_data'].copy()\n"
                "rate = sources['operation_rate_data'][['EQP_ID', 'EQP_EFFIC_CD', 'TOTAL_INTERVAL_RATE']].copy()\n"
                "result = assign.merge(rate, on='EQP_ID', how='left', validate='many_to_one')\n"
                "result = result[['EQP_ID', 'EQP_MODEL', 'OPER_NAME', 'TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'ORG', 'MCP_NO', 'DEVICE', 'EQP_EFFIC_CD', 'TOTAL_INTERVAL_RATE']].sort_values('EQP_ID')"
            ),
            [
                "EQP_ID",
                "EQP_MODEL",
                "OPER_NAME",
                "TECH",
                "DEN",
                "MODE",
                "PKG_TYPE1",
                "PKG_TYPE2",
                "LEAD",
                "ORG",
                "MCP_NO",
                "DEVICE",
                "EQP_EFFIC_CD",
                "TOTAL_INTERVAL_RATE",
            ],
            min_rows=3,
            expected_rows=[{"EQP_ID": "EQP002", "OPER_NAME": "D/A1", "TOTAL_INTERVAL_RATE": 81.5}],
        ),
        case(
            24,
            "D724장비 현재 가동율과 Event History 조회해줘",
            "equipment_utilization_and_event_history",
            [
                job("eqp_utilization_today", "utilization_data", "20260701", {"EQP_ID": eq("D724")}),
                job("eqp_history_today", "event_data", "20260701", {"EQP_ID": eq("D724")}),
            ],
            (
                "util = sources['utilization_data'][['EQP_ID', 'OPER_NAME', 'UTILIZATION_RATE', 'CURRENT_PRODUCT']].drop_duplicates()\n"
                "events = sources['event_data'][['EQP_ID', 'EVENT_TM', 'EVENT_CODE', 'EVENT_DESC', 'HIST_DESC']]\n"
                "result = util.merge(events, on='EQP_ID', how='left', validate='one_to_many').sort_values('EVENT_TM', ascending=False)"
            ),
            ["EQP_ID", "OPER_NAME", "UTILIZATION_RATE", "CURRENT_PRODUCT", "EVENT_TM", "EVENT_CODE", "EVENT_DESC", "HIST_DESC"],
            expected_rows=[{"EQP_ID": "D724", "EVENT_CODE": "RUN-START"}],
        ),
        case(
            25,
            "W/B공정 1일이상 Down된 장비 List와 Down시간, Down발생사유 알려줘",
            "wb_equipment_down_at_least_one_day",
            [job("eqp_down_list", "down_data", "20260701", {"OPER_NAME": in_values(WB_PROCESSES), "DOWN_TM": {"operator": "ge", "value": 24}})],
            (
                "df = sources['down_data'].copy()\n"
                "df['DOWN_TM'] = pd.to_numeric(df['DOWN_TM'], errors='coerce')\n"
                "result = df[df['DOWN_TM'].ge(24)][['EQP_ID', 'OPER_NAME', 'DOWN_TM', 'LAST_EVENT_DESC', 'HIST_DESC']].sort_values(['DOWN_TM', 'EQP_ID'], ascending=[False, True])"
            ),
            ["EQP_ID", "OPER_NAME", "DOWN_TM", "LAST_EVENT_DESC", "HIST_DESC"],
            expected_rows=[{"EQP_ID": "EQP-WB-DOWN-26H", "DOWN_TM": 26.0}, {"EQP_ID": "EQP-WB-DOWN-24H", "DOWN_TM": 24.0}],
            forbidden_values={"EQP_ID": ["EQP-WB-DOWN-12H"]},
        ),
    ]
    active_operational = [
        item
        for item in operational
        if int(item.get("id") or 0) not in EXCLUDED_OPERATIONAL_CASE_IDS
    ]
    return [*active_operational, *retained]


def case(
    case_id: int,
    question: str,
    analysis_kind: str,
    retrieval_jobs: list[dict[str, Any]],
    pandas_code: str,
    required_columns: list[str],
    *,
    min_rows: int = 1,
    expected_row_count: int | None = None,
    expected_first_row: dict[str, Any] | None = None,
    expected_rows: list[dict[str, Any]] | None = None,
    forbidden_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "id": case_id,
        "question": question,
        "intent_response": {
            "intent_plan": {
                "analysis_kind": analysis_kind,
                "retrieval_jobs": retrieval_jobs,
                "pandas_execution_plan": [{"step": analysis_kind}],
                "output_contract": {"required_columns": required_columns},
            },
            "metadata_refs": [],
            "trace": {"decision_reason": ["representative validation fixture"]},
        },
        "pandas_code": pandas_code,
        "required_columns": required_columns,
        "min_rows": min_rows,
    }
    if expected_row_count is not None:
        item["expected_row_count"] = expected_row_count
    if expected_first_row is not None:
        item["expected_first_row"] = deepcopy(expected_first_row)
    if expected_rows is not None:
        item["expected_rows"] = deepcopy(expected_rows)
    if forbidden_values is not None:
        item["forbidden_values"] = deepcopy(forbidden_values)
    return item


def product_case(
    case_id: int,
    question: str,
    analysis_kind: str,
    product_text: str,
    retrieval_jobs: list[dict[str, Any]],
    pandas_code: str,
    required_columns: list[str],
    *,
    min_rows: int = 1,
    expected_row_count: int | None = None,
    expected_first_row: dict[str, Any] | None = None,
    expected_rows: list[dict[str, Any]] | None = None,
    forbidden_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = case(
        case_id,
        question,
        analysis_kind,
        retrieval_jobs,
        pandas_code,
        required_columns,
        min_rows=min_rows,
        expected_row_count=expected_row_count,
        expected_first_row=expected_first_row,
        expected_rows=expected_rows,
        forbidden_values=forbidden_values,
    )
    item["intent_response"]["intent_plan"]["pandas_function_cases"] = [
        {
            "key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": product_text,
        }
    ]
    item["requires_helper"] = True
    item["helper_function"] = "match_product_tokens"
    return item


def ordered_range_case(
    case_id: int,
    question: str,
    analysis_kind: str,
    range_text: str,
    source_alias: str,
    retrieval_jobs: list[dict[str, Any]],
    pandas_code: str,
    required_columns: list[str],
    *,
    expected_row_count: int | None = None,
    expected_first_row: dict[str, Any] | None = None,
    expected_rows: list[dict[str, Any]] | None = None,
    forbidden_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """공정 순서 구간 helper의 선택·전달·실행을 한 번에 검증하는 대표 case를 만듭니다."""
    item = case(
        case_id,
        question,
        analysis_kind,
        retrieval_jobs,
        pandas_code,
        required_columns,
        expected_row_count=expected_row_count,
        expected_first_row=expected_first_row,
        expected_rows=expected_rows,
        forbidden_values=forbidden_values,
    )
    function_case = {
        "key": "ordered_process_range",
        "function_name": "filter_ordered_range",
        "input_text": range_text,
        "source_alias": source_alias,
    }
    item["intent_response"]["intent_plan"]["pandas_function_cases"] = [function_case]
    item["intent_response"]["intent_plan"]["pandas_execution_plan"] = [
        {
            "operation": "apply_pandas_function_case",
            "function_case_key": "ordered_process_range",
            "function_name": "filter_ordered_range",
            "input_text": range_text,
            "source_alias": source_alias,
        }
    ]
    item["requires_helper"] = True
    item["helper_function"] = "filter_ordered_range"
    return item


def job(
    dataset_key: str,
    source_alias: str,
    date: str = "",
    filters: dict[str, Any] | None = None,
    *,
    required_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = deepcopy(required_params or {})
    pandas_filters = deepcopy(filters or {})
    if date:
        if dataset_key in {
            "production_today",
            "production",
            "wip_today",
            "wip",
            "eqp_down_list",
            "eqp_utilization_today",
            "operation_rate_today",
            "eqp_history_today",
        }:
            params.setdefault("DATE", date)
        else:
            pandas_filters.setdefault("DATE", eq(date))
    return {
        "dataset_key": dataset_key,
        "source_alias": source_alias,
        "source_type": "goodocs" if dataset_key == "target" else "oracle",
        "required_params": params,
        "filters": pandas_filters,
    }


def eq(value: Any) -> dict[str, Any]:
    return {"operator": "eq", "value": value}


def in_values(values: list[Any]) -> dict[str, Any]:
    return {"operator": "in", "value": values}


def code_group_sum(alias: str, keys: list[str], value_column: str, output_column: str) -> str:
    return (
        f"result = sources[{alias!r}].groupby({keys!r}, as_index=False)[{value_column!r}].sum()"
        f".rename(columns={{{value_column!r}: {output_column!r}}})"
    )


def _capacity_by_product_code(assign_alias: str, uph_alias: str) -> str:
    """Build a validation-only CAPA calculation after the trusted join keys."""

    join_keys = ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
    return (
        f"join_keys = {join_keys!r}\n"
        f"assign = sources[{assign_alias!r}].copy()\n"
        f"uph = sources[{uph_alias!r}][join_keys + ['UPH']].copy()\n"
        "merged = assign.merge(uph, on=join_keys, how='left', validate='one_to_one')\n"
        f"result = merged.groupby({TARGET_PRODUCT_KEYS!r}, dropna=False, as_index=False).agg(EQP_COUNT=('EQP_ID', 'nunique'), AVG_UPH=('UPH', 'mean'))\n"
        "result['AVAILABLE_CAPA'] = (result['EQP_COUNT'] * result['AVG_UPH'] * 24).round(2)\n"
        "result = result.sort_values(['TECH', 'LEAD', 'MCP_NO'])"
    )


def _capacity_by_equipment_code(assign_alias: str, uph_alias: str) -> str:
    join_keys = ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
    return (
        f"join_keys = {join_keys!r}\n"
        f"assign = sources[{assign_alias!r}].copy()\n"
        f"uph = sources[{uph_alias!r}][join_keys + ['UPH']].copy()\n"
        "result = assign.merge(uph, on=join_keys, how='left', validate='one_to_one')\n"
        "result['AVAILABLE_CAPA'] = (result['UPH'] * 24).round(2)\n"
        "result = result[['OPER_NAME', 'EQP_ID', 'EQP_MODEL', 'RECIPE_ID', 'LEAD', 'UPH', 'AVAILABLE_CAPA']].sort_values(['OPER_NAME', 'EQP_ID'])"
    )


def _target_and_production_code(
    target_alias: str,
    production_alias: str,
    *,
    target_date: str,
    mcp_no: str = "",
) -> str:
    extra_filter = (
        f"\ntarget = target[target['MCP_NO'].astype(str).eq({mcp_no!r})]\n"
        f"prod = prod[prod['MCP_NO'].astype(str).eq({mcp_no!r})]"
        if mcp_no
        else ""
    )
    return (
        f"keys = {TARGET_PRODUCT_KEYS!r}\n"
        f"target = sources[{target_alias!r}].copy()\n"
        f"target = target[target['DATE'].astype(str).eq({target_date!r})]\n"
        f"prod = sources[{production_alias!r}].copy()"
        f"{extra_filter}\n"
        "target = target.groupby(keys, dropna=False, as_index=False)['OUT_PLAN'].sum()\n"
        "prod = prod.groupby(keys, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'PKG_OUT_QTY'})\n"
        "result = target.merge(prod, on=keys, how='left')\n"
        "result['PKG_OUT_QTY'] = result['PKG_OUT_QTY'].fillna(0)\n"
        "result = result.sort_values(['OUT_PLAN', 'MCP_NO'], ascending=[False, True])"
    )


def _target_and_input_code(target_alias: str, input_alias: str, *, target_date: str) -> str:
    return (
        f"keys = {TARGET_PRODUCT_KEYS!r}\n"
        f"target = sources[{target_alias!r}].copy()\n"
        f"target = target[target['DATE'].astype(str).eq({target_date!r})].copy()\n"
        "target['INPUT_PLAN'] = pd.to_numeric(target['INPUT_PLAN'], errors='coerce').fillna(0)\n"
        "target = target[target['INPUT_PLAN'].gt(0)].groupby(keys, dropna=False, as_index=False)['INPUT_PLAN'].sum()\n"
        f"actual = sources[{input_alias!r}].groupby(keys, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={{'PRODUCTION': 'INPUT_QTY'}})\n"
        "result = target.merge(actual, on=keys, how='left')\n"
        "result['INPUT_QTY'] = result['INPUT_QTY'].fillna(0)\n"
        "result = result.sort_values(['INPUT_PLAN', 'MCP_NO'], ascending=[False, True])"
    )


def _production_and_wip_by_grain_code(production_alias: str, wip_alias: str, grain: list[str]) -> str:
    return (
        f"grain = {grain!r}\n"
        f"prod = sources[{production_alias!r}].groupby(grain, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={{'PRODUCTION': 'PRODUCTION_QTY'}})\n"
        f"wip = sources[{wip_alias!r}].groupby(grain, dropna=False, as_index=False)['WIP'].sum().rename(columns={{'WIP': 'WIP_QTY'}})\n"
        "result = prod.merge(wip, on=grain, how='outer')\n"
        "result['PRODUCTION_QTY'] = result['PRODUCTION_QTY'].fillna(0)\n"
        "result['WIP_QTY'] = result['WIP_QTY'].fillna(0)\n"
        "result = result.sort_values(grain)"
    )


def _three_way_boh_production_code(boh_yesterday_alias: str, production_alias: str, boh_today_alias: str) -> str:
    return (
        f"keys = {TARGET_PRODUCT_KEYS!r}\n"
        f"boh_yesterday = sources[{boh_yesterday_alias!r}].groupby(keys, dropna=False, as_index=False)['WIP'].sum().rename(columns={{'WIP': 'BOH_YESTERDAY'}})\n"
        f"production = sources[{production_alias!r}].groupby(keys, dropna=False, as_index=False)['PRODUCTION'].sum().rename(columns={{'PRODUCTION': 'PRODUCTION_YESTERDAY'}})\n"
        f"boh_today = sources[{boh_today_alias!r}].groupby(keys, dropna=False, as_index=False)['WIP'].sum().rename(columns={{'WIP': 'BOH_TODAY'}})\n"
        "result = boh_yesterday.merge(production, on=keys, how='outer').merge(boh_today, on=keys, how='outer')\n"
        "for column in ['BOH_YESTERDAY', 'PRODUCTION_YESTERDAY', 'BOH_TODAY']:\n"
        "    result[column] = result[column].fillna(0)\n"
        "result = result.sort_values(keys)"
    )


def load_flow_modules() -> dict[str, Any]:
    return {
        "domain_loader": load_module(FLOW / "01a_mongodb_domain_metadata_loader.py"),
        "table_loader": load_module(FLOW / "01b_mongodb_table_catalog_loader.py"),
        "main_loader": load_module(FLOW / "01c_mongodb_main_variable_loader.py"),
        "candidates": load_module(FLOW / "01d_metadata_candidates_builder.py"),
        "request": load_module(FLOW / "00_analysis_request_loader.py"),
        "intent_vars": load_module(V2_FLOW / "02_intent_variables_builder.py"),
        "intent": load_module(V2_FLOW / "04_intent_plan_normalizer.py"),
        "hydrator": load_module(FLOW / "04a_trusted_retrieval_job_hydrator.py"),
        "validator": load_module(FLOW / "06_retrieval_job_validator.py"),
        "router": load_module(FLOW / "07_retrieval_job_router.py"),
        "dummy": load_module(FLOW / "08_dummy_data_retriever.py"),
        "merger": load_module(FLOW / "13_source_retrieval_merger.py"),
        "adapter": load_module(FLOW / "14_retrieval_payload_adapter.py"),
        "fast_resolver": load_module(V2_FLOW / "14b_simple_analysis_contract_resolver.py"),
        "pandas_vars": load_module(V2_FLOW / "16_route_aware_pandas_prompt_builder.py"),
        "helper_builder": load_module(FLOW / "15a_selected_helper_code_builder.py"),
        "executor": load_module(V2_FLOW / "17_hybrid_analysis_executor.py"),
        "answer_vars": load_module(V2_FLOW / "18_route_aware_answer_prompt_builder.py"),
        "answer_builder": load_module(V2_FLOW / "20_hybrid_answer_builder.py"),
        "message_adapter": load_module(V2_FLOW / "21_v2_answer_message_adapter.py"),
        "api_builder": load_module(FLOW / "22_api_response_builder.py"),
    }


def load_module(path: Path) -> Any:
    name = f"_validation_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_metadata_context(modules: dict[str, Any]) -> dict[str, Any]:
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    mongo_database = os.getenv("MONGODB_DATABASE", "datagov").strip() or "datagov"
    metadata_limit = os.getenv("VALIDATION_METADATA_LIMIT", "1000").strip() or "1000"
    domain = modules["domain_loader"].load_domain_metadata(
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=os.getenv("MONGODB_DOMAIN_COLLECTION", "agent_v4_domain_items").strip()
        or "agent_v4_domain_items",
        limit=metadata_limit,
    )
    table = modules["table_loader"].load_table_catalog_metadata(
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=os.getenv(
            "MONGODB_TABLE_CATALOG_COLLECTION",
            "agent_v4_table_catalog_items",
        ).strip()
        or "agent_v4_table_catalog_items",
        limit=metadata_limit,
    )
    main = modules["main_loader"].load_main_variable_metadata(
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=os.getenv(
            "MONGODB_MAIN_FLOW_FILTER_COLLECTION",
            "agent_v4_main_flow_filters",
        ).strip()
        or "agent_v4_main_flow_filters",
        limit=metadata_limit,
    )
    loads = [item.get("metadata_load", {}) for item in (domain, table, main) if isinstance(item, dict)]
    errors = [error for load in loads for error in load.get("errors", []) if isinstance(error, dict)]
    if errors:
        raise RuntimeError(f"metadata load failed: {errors}")
    return {"domain": domain, "table": table, "main": main}


def validation_catalog(case: dict[str, Any]) -> dict[str, Any]:
    jobs = case.get("intent_response", {}).get("intent_plan", {}).get("retrieval_jobs", [])
    items = []
    seen = set()
    for job_item in jobs if isinstance(jobs, list) else []:
        if not isinstance(job_item, dict):
            continue
        dataset_key = str(job_item.get("dataset_key") or "").strip()
        if not dataset_key or dataset_key in seen:
            continue
        seen.add(dataset_key)
        required_param_names = ["DATE"] if dataset_key in {
            "production_today",
            "production",
            "wip_today",
            "wip",
            "eqp_down_list",
            "eqp_utilization_today",
            "operation_rate_today",
            "eqp_history_today",
        } else []
        query_template = "SELECT * FROM DUMMY"
        if required_param_names:
            predicates = " AND ".join(f"{name} = {{{name}}}" for name in required_param_names)
            query_template = f"SELECT * FROM DUMMY WHERE {predicates}"
        if dataset_key == "target":
            # Keep the validation catalog aligned with the product grain used
            # by the representative reconciliation questions.  These are
            # canonical mappings for the synthetic target source, not a new
            # production metadata contract.
            filter_mappings = {
                "DATE": ["DATE"],
                "TECH": ["TECH"],
                "DEN": ["DEN"],
                "MODE": ["MODE"],
                "ORG": ["ORG"],
                "PKG_TYPE1": ["PKG_TYPE1"],
                "PKG_TYPE2": ["PKG_TYPE2"],
                "LEAD": ["LEAD"],
                "MCP_NO": ["MCP_NO"],
                "INPUT_PLAN_QTY": ["INPUT 계획"],
                "OUT_PLAN_QTY": ["OUT 계획"],
            }
        elif dataset_key == "eqp_uph":
            filter_mappings = {
                "EQP_MODEL": ["EQUIP_MODEL"],
                "OPER_NAME": ["OPER_NAME"],
                "RECIPE_ID": ["RECIPE_ID"],
                "LEAD": ["LEAD"],
                "MCP_NO": ["MCP_NO"],
                "UPH": ["UPH"],
            }
        elif dataset_key == "equipment_assign":
            filter_mappings = {
                "EQP_ID": ["EQUIP_ID"],
                "EQP_MODEL": ["EQUIP_MODEL"],
                "OPER_NAME": ["OPER_NM"],
                "RECIPE_ID": ["RECIPE_ID"],
                "LEAD": ["LEAD"],
                "MCP_NO": ["MCP_NO"],
            }
        elif dataset_key == "eqp_down_list":
            filter_mappings = {
                "DATE": ["DATE"],
                "EQP_ID": ["EQP_ID"],
                "OPER_NAME": ["OPER_NAME"],
                "DOWN_TM": ["DOWN_TM"],
                "EQP_STAT_CD": ["EQP_STAT_CD"],
            }
        elif dataset_key == "eqp_utilization_today":
            filter_mappings = {
                "DATE": ["DATE"],
                "EQP_ID": ["EQP_ID"],
                "EQP_MODEL": ["EQP_MODEL"],
                "OPER_NAME": ["OPER_NAME"],
                "RECIPE_ID": ["RECIPE_ID"],
                "LEAD": ["LEAD"],
                "MCP_NO": ["MCP_NO"],
                "UTILIZATION_RATE": ["UTILIZATION_RATE"],
            }
        elif dataset_key == "operation_rate_today":
            filter_mappings = {
                "DATE": ["WORK_DT"],
                "EQP_ID": ["EQP_ID"],
                "EQP_MODEL": ["EQP_MODEL_CD"],
                "EQP_EFFIC_CD": ["EQP_EFFIC_CD"],
                "TOTAL_INTERVAL_RATE": ["TOTAL_INTERVAL_RATE"],
            }
        elif dataset_key == "eqp_history_today":
            filter_mappings = {
                "DATE": ["DATE"],
                "EQP_ID": ["EQP_ID"],
                "OPER_NAME": ["OPER_NAME"],
                "EVENT_CODE": ["EVENT_CODE"],
            }
        elif dataset_key == "lot_status":
            filter_mappings = {
                "LOT_ID": ["LOT_ID"],
                "OPER_NAME": ["OPER_NAME"],
                "MCP_NO": ["MCP_NO"],
                "LEAD": ["LEAD"],
                "IN_TAT": ["IN_TAT"],
                "HOLD_STAT": ["HOLD_STAT"],
            }
        elif dataset_key == "hold_history":
            filter_mappings = {"LOT_ID": ["LOT_ID"], "OPER_NAME": ["OPER_NAME"]}
        elif required_param_names:
            filter_mappings = {"DATE": ["WORK_DATE"]}
        else:
            filter_mappings = {}
        items.append(
            {
                "dataset_key": dataset_key,
                "payload": {
                    "source_type": "goodocs" if dataset_key == "target" else "oracle",
                    "source_config": {"db_key": "VALIDATION_DUMMY", "query_template": query_template},
                    "required_params": required_param_names,
                    "filter_mappings": filter_mappings,
                },
            }
        )
    return {"table_catalog_items": items}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def resolve_llm_config() -> dict[str, Any]:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider != "gemini":
        raise RuntimeError(f"unsupported LLM_PROVIDER for this validator: {provider}")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_API_KEY, GEMINI_API_KEY, or GOOGLE_API_KEY is required for --use-llm")
    return {
        "api_key": api_key,
        "model": os.getenv("LLM_MODEL_NAME", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite",
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0") or 0),
        "timeout": int(float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or 60)),
    }


def render_prompt(path: Path, variables: dict[str, Any]) -> str:
    return path.read_text(encoding="utf-8").format(**variables)


def call_llm(prompt: str, config: dict[str, Any]) -> str:
    model = str(config["model"]).removeprefix("models/")
    encoded_model = urllib.parse.quote(model, safe="")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent?key={urllib.parse.quote(str(config['api_key']), safe='')}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": config["temperature"],
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(
                error_payload.get("error", {}).get("message")
                if isinstance(error_payload, dict)
                else ""
            ).strip()
        except Exception:
            detail = ""
        suffix = f": {detail[:500]}" if detail else ""
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not text.strip():
        raise RuntimeError("LLM response did not contain text")
    return text


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=json_default))


def json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def install_lfx_stubs() -> None:
    if importlib.util.find_spec("lfx") is not None:
        return

    class Component:
        pass

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(self, text=""):
            self.text = text

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    modules = {
        "lfx": types.ModuleType("lfx"),
        "lfx.custom": types.ModuleType("lfx.custom"),
        "lfx.custom.custom_component": types.ModuleType("lfx.custom.custom_component"),
        "lfx.custom.custom_component.component": types.ModuleType("lfx.custom.custom_component.component"),
        "lfx.io": types.ModuleType("lfx.io"),
        "lfx.schema": types.ModuleType("lfx.schema"),
        "lfx.schema.data": types.ModuleType("lfx.schema.data"),
        "lfx.schema.message": types.ModuleType("lfx.schema.message"),
    }
    modules["lfx.custom.custom_component.component"].Component = Component
    modules["lfx.io"].DataInput = InputBase
    modules["lfx.io"].DropdownInput = InputBase
    modules["lfx.io"].BoolInput = InputBase
    modules["lfx.io"].IntInput = InputBase
    modules["lfx.io"].MessageTextInput = InputBase
    modules["lfx.io"].ModelInput = InputBase
    modules["lfx.io"].MultilineInput = InputBase
    modules["lfx.io"].Output = InputBase
    modules["lfx.io"].SecretStrInput = InputBase
    modules["lfx.schema.data"].Data = Data
    modules["lfx.schema.message"].Message = Message
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


if __name__ == "__main__":
    raise SystemExit(main())
