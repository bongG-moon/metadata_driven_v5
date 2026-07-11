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
FLOW = ROOT / "langflow_components" / "data_analysis_flow"

PRODUCT_KEYS = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO", "DEVICE"]
TARGET_PRODUCT_KEYS = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
DA_PROCESSES = ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]
WB_PROCESSES = ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"]
FCB_PROCESSES = ["FCB1", "FCB2", "FCB/H"]
BG_PROCESSES = ["B/G1", "B/G2"]
MOBILE_PKGS = ["LFBGA", "TFBGA", "UFBGA", "VFBGA", "WFBGA"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Langflow-like validation for the representative manufacturing question set.")
    parser.add_argument("--json", action="store_true", help="Print full validation result as JSON.")
    parser.add_argument("--use-llm", action="store_true", help="Use .env MongoDB metadata and LLM settings to run prompt -> LLM -> flow validation.")
    parser.add_argument("--limit", type=int, default=0, help="Validate only the first N cases.")
    parser.add_argument("--ids", default="", help="Comma-separated case ids to validate, for example: 3,8,13.")
    parser.add_argument("--reference-date", default="", help="Override request.reference_date for this validation run. Defaults to VALIDATION_REFERENCE_DATE or 20260701.")
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
    if args.use_llm:
        metadata_context = load_metadata_context(modules)
        llm_config = resolve_llm_config()
        results = [run_llm_case(case, modules, metadata_context, llm_config, reference_date) for case in cases]
    else:
        results = [run_case(case, modules, reference_date) for case in cases]
    failed = [item for item in results if item["status"] != "ok"]

    if args.json:
        print(json.dumps({"status": "ok" if not failed else "error", "results": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            marker = "OK" if item["status"] == "ok" else "FAIL"
            print(f"[{marker}] {item['id']}. {item['question']}")
            print(f"  intent={item['analysis_kind']} jobs={item['retrieval_job_count']} rows={item['row_count']}")
            print(f"  columns={', '.join(item['columns'])}")
            if item.get("errors"):
                print(f"  errors={item['errors']}")
        print(f"\nsummary: {len(results) - len(failed)}/{len(results)} passed")

    return 1 if failed else 0


def run_llm_case(case: dict[str, Any], modules: dict[str, Any], metadata_context: dict[str, Any], llm_config: dict[str, Any], reference_date: str) -> dict[str, Any]:
    payload = build_validation_request(case["question"], modules, reference_date)
    candidates_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    metadata_candidates = candidates_payload.get("metadata_candidates", candidates_payload)
    intent_vars = with_specialized_prompt(modules["intent_vars"].build_variables(payload, metadata_candidates))
    intent_prompt = render_prompt(FLOW / "03_intent_prompt_template_ko.md", intent_vars)
    intent_response = call_llm(intent_prompt, llm_config)
    payload = modules["intent"].normalize_intent_plan(payload, intent_response)
    payload = modules["hydrator"].hydrate_retrieval_jobs(payload, metadata_context["table"], retrieval_mode="dummy")
    payload = modules["validator"].validate_retrieval_payload(payload)
    dummy_bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    dummy_result = modules["dummy"].retrieve_dummy_data(dummy_bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, dummy_result)
    payload = modules["adapter"].build_retrieval_payload(payload)

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
    return summarize_validation_result(case, selected_payload, pandas_vars, strict_columns=False, api_response=api_response)


def run_case(case: dict[str, Any], modules: dict[str, Any], reference_date: str) -> dict[str, Any]:
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
    pandas_code = inline_helper_source(case["pandas_code"]) if case.get("requires_helper") else case["pandas_code"]
    payload = modules["executor"].execute_pandas_code(payload, {"code": pandas_code})
    row_count = int(payload.get("data", {}).get("row_count") or 0)
    payload = modules["answer_builder"].build_answer_response(
        payload,
        f"[더미 데이터] '{case['question']}' 분석 결과는 {row_count}건입니다.",
    )
    display_message = modules["message_adapter"].build_message(payload)
    api_response = modules["api_builder"].build_api_response(payload, display_message)
    return summarize_validation_result(case, payload, pandas_vars, strict_columns=True, api_response=api_response)


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


def inline_helper_source(pandas_code: str) -> str:
    source = function_case_source("match_product_tokens")
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
    strict_columns: bool,
    api_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors = []
    warnings = []
    if payload.get("analysis", {}).get("status") != "ok":
        errors.append(payload.get("analysis", {}).get("error", {}).get("message", "pandas execution failed"))
    if not payload.get("intent_plan", {}).get("retrieval_jobs"):
        errors.append("missing retrieval_jobs")
    if payload.get("analysis", {}).get("row_count", 0) < case.get("min_rows", 1):
        errors.append(f"row_count < {case.get('min_rows', 1)}")
    actual_rows = payload.get("data", {}).get("rows", [])
    expected_row_count = case.get("expected_row_count")
    if expected_row_count is not None and len(actual_rows) != int(expected_row_count):
        errors.append(f"row_count != {expected_row_count}")
    expected_first_row = case.get("expected_first_row")
    if isinstance(expected_first_row, dict):
        if not actual_rows:
            errors.append("missing expected first row")
        else:
            for key, expected_value in expected_first_row.items():
                if actual_rows[0].get(key) != expected_value:
                    errors.append(f"first row {key} != {expected_value!r}")
    expected_rows = case.get("expected_rows")
    if isinstance(expected_rows, list):
        for expected_index, expected_row in enumerate(expected_rows, start=1):
            if not isinstance(expected_row, dict):
                continue
            matched = any(
                all(actual_row.get(key) == expected_value for key, expected_value in expected_row.items())
                for actual_row in actual_rows
                if isinstance(actual_row, dict)
            )
            if not matched:
                errors.append(f"missing expected row #{expected_index}: {expected_row!r}")
    forbidden_values = case.get("forbidden_values")
    if isinstance(forbidden_values, dict):
        for column, values in forbidden_values.items():
            blocked_values = values if isinstance(values, list) else [values]
            found = [row.get(column) for row in actual_rows if isinstance(row, dict) and row.get(column) in blocked_values]
            if found:
                errors.append(f"forbidden {column} values present: {found!r}")
    for column in case.get("required_columns", []):
        if column not in payload.get("analysis", {}).get("columns", []):
            if strict_columns:
                errors.append(f"missing column: {column}")
            else:
                warnings.append(f"missing expected fixture column: {column}")
    function_case_text = json.dumps(
        {
            "selection": pandas_vars.get("function_case_selection_json", ""),
            "helper_code": pandas_vars.get("function_case_helper_code", ""),
        },
        ensure_ascii=False,
    )
    if case.get("requires_helper") and "match_product_tokens" not in function_case_text:
        errors.append("missing match_product_tokens function case context")
    actual_kind = payload.get("intent_plan", {}).get("analysis_kind", "")
    expected_kind = case.get("intent_response", {}).get("intent_plan", {}).get("analysis_kind", "")
    if actual_kind != expected_kind:
        errors.append(f"analysis_kind != {expected_kind!r}")
    if (api_response or {}).get("data_mode") != "dummy":
        errors.append("data_mode != 'dummy'")

    pandas_trace = payload.get("trace", {}).get("inspection", {}).get("pandas_execution", {})
    result = {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
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
    }
    return json_safe(result)


def representative_cases() -> list[dict[str, Any]]:
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
                "result['WIP_PER_INPUT'] = result['TOTAL_WIP'] / result['INPUT_QTY']\n"
                "result = result.sort_values(['WIP_PER_INPUT', 'TOTAL_WIP'], ascending=[False, False]).head(5)"
            ),
            ["INPUT_QTY", "TOTAL_WIP", "WIP_PER_INPUT", "DEVICE"],
            expected_row_count=5,
            expected_first_row={"DEVICE": "DEV-RANK-1", "INPUT_QTY": 100, "TOTAL_WIP": 1200, "WIP_PER_INPUT": 12.0},
            forbidden_values={"DEVICE": ["DEV-RANK-6"]},
        ),
        case(
            11,
            "7/1 현시간 기준 Input 실적은 있으나 D/A 공정 WIP 없는 제품 확인해줘",
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
                "result = merged[merged['_merge'].eq('left_only')].drop(columns=['_merge']).sort_values('INPUT_QTY', ascending=False)"
            ),
            ["INPUT_QTY", "DEVICE"],
            expected_row_count=6,
            expected_first_row={"DEVICE": "DEV-L218K8H", "INPUT_QTY": 440},
            expected_rows=[{"DEVICE": "DEV-ZERO-DA-WIP", "INPUT_QTY": 50}],
            forbidden_values={"DEVICE": ["DEV-ZERO-INPUT"]},
        ),
        case(
            12,
            "전일 L-218K8H 제품의 SBM공정에서 생산 실적 알려줘",
            "l218k8h_sbm_previous_day_production",
            [job("production", "production_data", "20260630", {"OPER_NAME": eq("SBM"), "MCP_NO": eq("L-218K8H")})],
            code_group_sum("production_data", PRODUCT_KEYS, "PRODUCTION", "TOTAL_PRODUCTION"),
            ["TOTAL_PRODUCTION", "MCP_NO", "DEVICE"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV-L218K8H", "MCP_NO": "L-218K8H", "TOTAL_PRODUCTION": 678},
            forbidden_values={"DEVICE": ["DEV-L218-PREFIX-DECOY"]},
        ),
        product_case(
            13,
            "오늘 아침 07시 기준 DA 16G GDDR6 180 제품 재공 수량 알려줘",
            "da_gddr6_today_boh_wip",
            "DA 16G GDDR6 180",
            [job("wip", "wip_data", "20260630", {"SNAPSHOT_TIME": eq("07:00")})],
            (
                "df = match_product_tokens('DA 16G GDDR6 180', sources['wip_data'])\n"
                "result = df.groupby(['TECH', 'DEN', 'MODE', 'PKG_TYPE1', 'PKG_TYPE2', 'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['WIP'].sum().rename(columns={'WIP': 'BOH_WIP'})"
            ),
            ["BOH_WIP", "DEVICE"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV-DA-GDDR6", "BOH_WIP": 224},
        ),
        case(
            14,
            "7월 1일 제품별 INPUT 계획과 OUT 계획을 OUT 계획이 큰 순서로 알려줘",
            "target_plan_by_product",
            [job("target", "target_data", "20260701")],
            (
                f"columns = {TARGET_PRODUCT_KEYS!r} + ['INPUT_PLAN', 'OUT_PLAN']\n"
                "result = sources['target_data'].reindex(columns=columns).sort_values(['OUT_PLAN', 'TECH'], ascending=[False, True])"
            ),
            ["INPUT_PLAN", "OUT_PLAN", "TECH"],
            expected_row_count=8,
            expected_first_row={"TECH": "1Z", "INPUT_PLAN": 800, "OUT_PLAN": 1200},
        ),
        case(
            15,
            "현재 장비 모델별 보유 장비 대수를 알려줘. 장비 ID 중복은 제외해줘",
            "distinct_equipment_count_by_model",
            [job("equipment_assign", "equipment_data")],
            (
                "df = sources['equipment_data']\n"
                "result = df.groupby('EQP_MODEL', as_index=False)['EQP_ID'].nunique().rename(columns={'EQP_ID': 'EQP_COUNT'}).sort_values(['EQP_COUNT', 'EQP_MODEL'], ascending=[False, True])"
            ),
            ["EQP_MODEL", "EQP_COUNT"],
            expected_row_count=5,
            expected_first_row={"EQP_MODEL": "EQM-A", "EQP_COUNT": 2},
            expected_rows=[
                {"EQP_MODEL": "EQM-HBM", "EQP_COUNT": 2},
                {"EQP_MODEL": "EQM-BG", "EQP_COUNT": 1},
            ],
        ),
        case(
            16,
            "장비 모델별 평균 UPH를 낮은 순서로 보여줘",
            "average_uph_by_equipment_model",
            [job("eqp_uph", "uph_data")],
            (
                "df = sources['uph_data']\n"
                "result = df.groupby('EQP_MODEL', as_index=False)['UPH'].mean().rename(columns={'UPH': 'AVG_UPH'})\n"
                "result['AVG_UPH'] = result['AVG_UPH'].round(2)\n"
                "result = result.sort_values(['AVG_UPH', 'EQP_MODEL'])"
            ),
            ["EQP_MODEL", "AVG_UPH"],
            expected_row_count=5,
            expected_first_row={"EQP_MODEL": "EQM-HBM", "AVG_UPH": 92.73},
        ),
        case(
            17,
            "현재 HOLD 중인 LOT 목록과 LOT별 UNIT 수량, Wafer 수량, 현재·누적 TAT를 보여줘",
            "current_hold_lot_details",
            [job("lot_status", "lot_data", filters={"HOLD_STAT": eq("OnHold")})],
            (
                "df = sources['lot_data'].copy()\n"
                "result = df[['LOT_ID', 'DEVICE', 'PROD_QTY', 'WF_QTY', 'IN_TAT', 'CUM_TAT', 'HOLD_STAT', 'HOLD_REASON']].rename(columns={'PROD_QTY': 'UNIT_QTY', 'WF_QTY': 'WAFER_QTY'}).sort_values('LOT_ID')"
            ),
            ["LOT_ID", "UNIT_QTY", "WAFER_QTY", "IN_TAT", "CUM_TAT"],
            expected_row_count=1,
            expected_first_row={"LOT_ID": "T1234567GEN1", "UNIT_QTY": 100, "WAFER_QTY": 25, "IN_TAT": 12.5, "CUM_TAT": 40.0},
        ),
        case(
            18,
            "7월 1일 제품별 INPUT 계획 대비 실제 INPUT 실적과 달성률을 알려줘",
            "input_plan_vs_actual_achievement",
            [
                job("target", "target_data", "20260701"),
                job("production_today", "input_data", "20260701", {"OPER_NAME": eq("INPUT")}),
            ],
            (
                f"keys = {TARGET_PRODUCT_KEYS!r}\n"
                "target = sources['target_data'].groupby(keys, as_index=False)['INPUT_PLAN'].sum()\n"
                "actual = sources['input_data'].groupby(keys + ['DEVICE'], as_index=False)['PRODUCTION'].sum().rename(columns={'PRODUCTION': 'INPUT_ACTUAL'})\n"
                "result = target.merge(actual, on=keys, how='left')\n"
                "result['INPUT_ACTUAL'] = result['INPUT_ACTUAL'].fillna(0)\n"
                "result['SHORTFALL'] = result['INPUT_PLAN'] - result['INPUT_ACTUAL']\n"
                "result['ACHIEVEMENT_RATE'] = (result['INPUT_ACTUAL'] / result['INPUT_PLAN'] * 100).round(2)\n"
                "result = result.sort_values(['ACHIEVEMENT_RATE', 'TECH'])"
            ),
            ["DEVICE", "INPUT_PLAN", "INPUT_ACTUAL", "SHORTFALL", "ACHIEVEMENT_RATE"],
            expected_row_count=8,
            expected_first_row={"TECH": "1Z", "DEVICE": "DEV001", "INPUT_PLAN": 800, "INPUT_ACTUAL": 181},
        ),
        case(
            19,
            "현재 D/A1 공정에 배정된 장비와 해당 Recipe의 UPH를 함께 보여줘",
            "da1_equipment_recipe_uph",
            [
                job("equipment_assign", "equipment_data", filters={"OPER_NAME": eq("D/A1")}),
                job("eqp_uph", "uph_data", filters={"OPER_NAME": eq("D/A1")}),
            ],
            (
                "assign = sources['equipment_data']\n"
                "uph = sources['uph_data'][['EQP_MODEL', 'RECIPE_ID', 'OPER_NAME', 'UPH']]\n"
                "result = assign.merge(uph, on=['EQP_MODEL', 'RECIPE_ID', 'OPER_NAME'], how='left')\n"
                "result = result[['EQP_ID', 'EQP_MODEL', 'RECIPE_ID', 'OPER_NAME', 'PRESS_CNT', 'UPH']].sort_values('EQP_ID')"
            ),
            ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "PRESS_CNT", "UPH"],
            expected_row_count=1,
            expected_first_row={"EQP_ID": "EQP002", "EQP_MODEL": "EQM-HBM", "RECIPE_ID": "RCP-002", "PRESS_CNT": 4, "UPH": 88.2},
        ),
        case(
            20,
            "현재 HOLD 중인 LOT별 가장 최근 HOLD 코드와 상세 사유를 알려줘",
            "current_hold_latest_history",
            [
                job("lot_status", "lot_data", filters={"HOLD_STAT": eq("OnHold")}),
                job("hold_history", "hold_data"),
            ],
            (
                "holds = sources['lot_data'][['LOT_ID', 'DEVICE', 'HOLD_STAT']]\n"
                "history = sources['hold_data'].sort_values(['LOT_ID', 'HOLD_TM']).drop_duplicates('LOT_ID', keep='last')\n"
                "result = holds.merge(history[['LOT_ID', 'HOLD_TM', 'HOLD_CD', 'HOLD_DESC']], on='LOT_ID', how='left').sort_values('LOT_ID')"
            ),
            ["LOT_ID", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
            expected_row_count=1,
            expected_first_row={"LOT_ID": "T1234567GEN1", "HOLD_CD": "H001", "HOLD_DESC": "검증용 HOLD 이력"},
            forbidden_values={"HOLD_CD": ["H000"]},
        ),
        case(
            21,
            "7월 1일 DA 공정 재공이 가장 많은 제품의 현재 LOT 수, UNIT 수량, Wafer 수량을 알려줘",
            "top_da_wip_product_lot_summary",
            [
                job("wip_today", "da_wip_data", "20260701", {"OPER_NAME": in_values(DA_PROCESSES)}),
                job("lot_status", "lot_data"),
            ],
            (
                "wip = sources['da_wip_data'].groupby('DEVICE', as_index=False)['WIP'].sum().rename(columns={'WIP': 'TOP_DA_WIP'}).sort_values(['TOP_DA_WIP', 'DEVICE'], ascending=[False, True]).head(1)\n"
                "lots = sources['lot_data'].groupby('DEVICE', as_index=False).agg(LOT_COUNT=('LOT_ID', 'nunique'), UNIT_QTY=('PROD_QTY', 'sum'), WAFER_QTY=('WF_QTY', 'sum'))\n"
                "result = wip.merge(lots, on='DEVICE', how='left')"
            ),
            ["DEVICE", "TOP_DA_WIP", "LOT_COUNT", "UNIT_QTY", "WAFER_QTY"],
            expected_row_count=1,
            expected_first_row={"DEVICE": "DEV002", "TOP_DA_WIP": 891, "LOT_COUNT": 2, "UNIT_QTY": 130, "WAFER_QTY": 34},
        ),
        case(
            22,
            "LOT T9999999GEN1의 현재 상태와 TAT를 알려줘",
            "lot_status_not_found",
            [job("lot_status", "lot_data", filters={"LOT_ID": eq("T9999999GEN1")})],
            "result = sources['lot_data'].reindex(columns=['LOT_ID', 'LOT_STAT', 'HOLD_STAT', 'IN_TAT', 'CUM_TAT'])",
            ["LOT_ID", "LOT_STAT", "HOLD_STAT", "IN_TAT", "CUM_TAT"],
            min_rows=0,
            expected_row_count=0,
        ),
        case(
            23,
            "7월 2일 제품별 OUT 계획을 알려줘",
            "target_plan_not_found",
            [job("target", "target_data", "20260702")],
            f"result = sources['target_data'].reindex(columns={TARGET_PRODUCT_KEYS!r} + ['OUT_PLAN'])",
            ["OUT_PLAN", "TECH"],
            min_rows=0,
            expected_row_count=0,
        ),
    ]


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
    if date:
        params.setdefault("DATE", date)
    return {
        "dataset_key": dataset_key,
        "source_alias": source_alias,
        "source_type": "oracle",
        "required_params": params,
        "filters": deepcopy(filters or {}),
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


def load_flow_modules() -> dict[str, Any]:
    return {
        "domain_loader": load_module(FLOW / "01a_mongodb_domain_metadata_loader.py"),
        "table_loader": load_module(FLOW / "01b_mongodb_table_catalog_loader.py"),
        "main_loader": load_module(FLOW / "01c_mongodb_main_variable_loader.py"),
        "candidates": load_module(FLOW / "01d_metadata_candidates_builder.py"),
        "request": load_module(FLOW / "00_analysis_request_loader.py"),
        "intent_vars": load_module(FLOW / "02_intent_variables_builder.py"),
        "intent": load_module(FLOW / "04_intent_plan_normalizer.py"),
        "hydrator": load_module(FLOW / "04a_trusted_retrieval_job_hydrator.py"),
        "validator": load_module(FLOW / "06_retrieval_job_validator.py"),
        "router": load_module(FLOW / "07_retrieval_job_router.py"),
        "dummy": load_module(FLOW / "08_dummy_data_retriever.py"),
        "merger": load_module(FLOW / "13_source_retrieval_merger.py"),
        "adapter": load_module(FLOW / "14_retrieval_payload_adapter.py"),
        "pandas_vars": load_module(FLOW / "15_pandas_variables_builder.py"),
        "helper_builder": load_module(FLOW / "15a_selected_helper_code_builder.py"),
        "executor": load_module(FLOW / "17_pandas_code_executor.py"),
        "answer_vars": load_module(FLOW / "18_answer_variables_builder.py"),
        "answer_builder": load_module(FLOW / "20_answer_response_builder.py"),
        "message_adapter": load_module(FLOW / "21_answer_message_adapter.py"),
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
    domain = modules["domain_loader"].load_domain_metadata(limit=os.getenv("VALIDATION_METADATA_LIMIT", "1000"))
    table = modules["table_loader"].load_table_catalog_metadata(limit=os.getenv("VALIDATION_METADATA_LIMIT", "1000"))
    main = modules["main_loader"].load_main_variable_metadata(limit=os.getenv("VALIDATION_METADATA_LIMIT", "1000"))
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
        required_param_names = list(job_item.get("required_params", {}))
        query_template = "SELECT * FROM DUMMY"
        if required_param_names:
            predicates = " AND ".join(f"{name} = {{{name}}}" for name in required_param_names)
            query_template = f"SELECT * FROM DUMMY WHERE {predicates}"
        items.append(
            {
                "dataset_key": dataset_key,
                "payload": {
                    "source_type": "oracle",
                    "source_config": {"db_key": "VALIDATION_DUMMY", "query_template": query_template},
                    "required_params": required_param_names,
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
        "model": os.getenv("LLM_MODEL_NAME", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
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
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}") from exc
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
