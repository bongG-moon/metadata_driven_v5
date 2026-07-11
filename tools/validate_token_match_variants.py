from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_representative_questions as representative


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Langflow-like validation for product token match variants.")
    parser.add_argument("--json", action="store_true", help="Print full validation result as JSON.")
    parser.add_argument("--reference-date", default="", help="Override request.reference_date. Defaults to VALIDATION_REFERENCE_DATE or 20260701.")
    args = parser.parse_args()

    representative.load_dotenv(ROOT / ".env")
    reference_date = args.reference_date.strip() or os.getenv("VALIDATION_REFERENCE_DATE", "").strip() or "20260701"
    representative.install_lfx_stubs()
    modules = representative.load_flow_modules()
    results = [run_case(case, modules, reference_date) for case in token_variant_cases()]
    failed = [item for item in results if item["status"] != "ok"]

    output = {
        "status": "ok" if not failed else "error",
        "reference_date": reference_date,
        "flow_sequence": ["00", "04", "06", "07", "08", "13", "14", "15", "17"],
        "results": results,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("flow sequence: 00 -> 04 -> 06 -> 07 -> 08 -> 13 -> 14 -> 15 -> 17")
        print(f"reference_date: {reference_date}\n")
        for item in results:
            marker = "OK" if item["status"] == "ok" else "FAIL"
            print(f"[{marker}] {item['id']}: {item['question']}")
            print(f"  input_text={item['function_case']['input_text']}")
            print(f"  retrieval={item['retrieval']['dataset_key']} rows={item['retrieval']['row_count']} filters={json.dumps(item['retrieval']['pandas_filters'], ensure_ascii=False)}")
            print(f"  matched_devices={item['matched_devices']}")
            if item["matched_mcp"]:
                print(f"  matched_mcp={item['matched_mcp']}")
            print(f"  helper_used={item['helper_used']} filter_preamble={item['filter_preamble_applied']} rows={item['row_count']}")
            if item["errors"]:
                print(f"  errors={item['errors']}")
        print(f"\nsummary: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def run_case(case: dict[str, Any], modules: dict[str, Any], reference_date: str) -> dict[str, Any]:
    payload = representative.build_validation_request(case["question"], modules, reference_date)
    payload = modules["intent"].normalize_intent_plan(payload, build_intent_response(case, reference_date))
    payload.setdefault("request", {})["retrieval_mode"] = "dummy"
    payload = modules["validator"].validate_retrieval_payload(payload)
    dummy_bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    dummy_result = modules["dummy"].retrieve_dummy_data(dummy_bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, dummy_result)
    payload = modules["adapter"].build_retrieval_payload(payload)
    pandas_vars = representative.with_selected_helper_code(modules, modules["pandas_vars"].build_variables(payload))
    pandas_code = representative.inline_helper_source(case["pandas_code"])
    payload = modules["executor"].execute_pandas_code(payload, {"code": pandas_code})
    return summarize_case(case, payload, pandas_vars)


def build_intent_response(case: dict[str, Any], reference_date: str) -> dict[str, Any]:
    function_case = {
        "key": "product_token_match",
        "function_name": "match_product_tokens",
        "input_text": case["input_text"],
        "source_alias": "product_data",
    }
    return {
        "intent_plan": {
            "analysis_kind": case["id"],
            "retrieval_jobs": [
                {
                    "dataset_key": "product_token_fixture",
                    "source_alias": "product_data",
                    "source_type": "oracle",
                    "required_params": {"DATE": reference_date},
                    "filters": deepcopy(case["filters"]),
                }
            ],
            "pandas_function_cases": [function_case],
            "pandas_execution_plan": [
                {
                    "step": "제품 token match helper 적용",
                    "operation": "apply_pandas_function_case",
                    "function_case_key": "product_token_match",
                    "function_name": "match_product_tokens",
                    "input_text": case["input_text"],
                    "source_alias": "product_data",
                },
                {"step": "검증 결과 컬럼 선택"},
            ],
            "output_contract": {"required_columns": case["columns"]},
        },
        "metadata_refs": [{"section": "pandas_function_cases", "key": "product_token_match"}],
        "trace": {"decision_reason": ["token variant validation fixture"]},
    }


def summarize_case(case: dict[str, Any], payload: dict[str, Any], pandas_vars: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    rows = payload.get("data", {}).get("rows", [])
    matched_devices = [str(row.get("DEVICE", "")) for row in rows if isinstance(row, dict)]
    matched_mcp = [str(row.get("MCP_NO", "")) for row in rows if isinstance(row, dict) and row.get("MCP_NO") not in (None, "")]
    if payload.get("analysis", {}).get("status") != "ok":
        errors.append(payload.get("analysis", {}).get("error", {}).get("message", "pandas execution failed"))
    if matched_devices != case["expected_devices"]:
        errors.append(f"matched_devices mismatch: expected={case['expected_devices']} actual={matched_devices}")
    if case.get("expected_mcp") and matched_mcp != case["expected_mcp"]:
        errors.append(f"matched_mcp mismatch: expected={case['expected_mcp']} actual={matched_mcp}")
    selection_text = pandas_vars.get("function_case_selection_json", "")
    helper_code = pandas_vars.get("function_case_helper_code", "")
    if "match_product_tokens" not in selection_text or "def match_product_tokens" not in helper_code:
        errors.append("missing function case selection/helper context")
    pandas_trace = payload.get("trace", {}).get("inspection", {}).get("pandas_execution", {})
    retrieval = next((item for item in payload.get("source_results", []) if isinstance(item, dict)), {})
    return representative.json_safe(
        {
            "id": case["id"],
            "question": case["question"],
            "status": "ok" if not errors else "error",
            "function_case": {
                "function_name": "match_product_tokens",
                "input_text": case["input_text"],
                "source_alias": "product_data",
            },
            "retrieval": {
                "dataset_key": retrieval.get("dataset_key", ""),
                "source_alias": retrieval.get("source_alias", ""),
                "row_count": retrieval.get("row_count", 0),
                "applied_params": retrieval.get("applied_params", {}),
                "pandas_filters": retrieval.get("pandas_filters", {}),
            },
            "matched_devices": matched_devices,
            "matched_mcp": matched_mcp,
            "row_count": payload.get("analysis", {}).get("row_count", 0),
            "helper_used": "match_product_tokens" in payload.get("analysis", {}).get("used_helpers", []),
            "filter_preamble_applied": bool(pandas_trace.get("pandas_filter_preamble")),
            "generated_code": pandas_trace.get("generated_code", ""),
            "errors": errors,
        }
    )


def token_variant_cases() -> list[dict[str, Any]]:
    return [
        token_case(
            case_id="multi_org_x",
            question="RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP 제품의 WB공정 차수별 재공 알려줘",
            input_text="RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP",
            filters={"OPER_NAME": {"operator": "in", "value": ["W/B1"]}},
            columns=["DEVICE", "ORG", "LEAD", "PKG1", "PKG2", "MCP_NO", "WIP"],
            expected_devices=["RG-X16", "CP-X8"],
        ),
        token_case(
            case_id="uppercase_x8",
            question="CP 16G DDR X8 78 FCBGA SDP 제품 INPUT 수량 알려줘",
            input_text="CP 16G DDR X8 78 FCBGA SDP",
            filters={"OPER_NAME": {"operator": "eq", "value": "INPUT"}},
            columns=["DEVICE", "ORG", "LEAD", "PKG1", "MCP_NO", "PRODUCTION"],
            expected_devices=["CP-X8"],
        ),
        token_case(
            case_id="fc78_pkg_and_lead",
            question="FC78 제품 WB공정 재공 알려줘",
            input_text="FC78",
            filters={"OPER_NAME": {"operator": "eq", "value": "W/B1"}},
            columns=["DEVICE", "PKG1", "LEAD", "WIP"],
            expected_devices=["CP-X8", "DEV-SP-DDR5-FCBGA78"],
        ),
        token_case(
            case_id="f78_lead_only",
            question="F78 제품 WB공정 재공 알려줘",
            input_text="F78",
            filters={"OPER_NAME": {"operator": "eq", "value": "W/B1"}},
            columns=["DEVICE", "PKG1", "LEAD", "WIP"],
            expected_devices=["CP-X8", "CP-F78-V", "DEV-SP-DDR5-FCBGA78"],
        ),
        token_case(
            case_id="fc96_pkg_and_lead",
            question="FC96 제품 WB공정 재공 알려줘",
            input_text="FC96",
            filters={"OPER_NAME": {"operator": "eq", "value": "W/B1"}},
            columns=["DEVICE", "PKG1", "LEAD", "WIP"],
            expected_devices=["RG-X16", "RG-WRONG-ORG", "CP-WRONG-LEAD"],
        ),
        token_case(
            case_id="f96_lead_only",
            question="F96 제품 WB공정 재공 알려줘",
            input_text="F96",
            filters={"OPER_NAME": {"operator": "eq", "value": "W/B1"}},
            columns=["DEVICE", "PKG1", "LEAD", "WIP"],
            expected_devices=["RG-X16", "RG-F96-V", "RG-WRONG-ORG", "CP-WRONG-LEAD"],
        ),
        token_case(
            case_id="mcp_partial_prefixes",
            question="L-218, L-216, A-663 제품 PKG 투입수량 알려줘",
            input_text="L-218, L-216, A-663",
            filters={"OPER_NAME": {"operator": "eq", "value": "INPUT"}},
            columns=["DEVICE", "MCP_NO", "PRODUCTION"],
            expected_devices=["RG-X16", "CP-X8", "CP-F78-V"],
            expected_mcp=["L-218K8H", "L-216A1", "A-663Z9"],
        ),
    ]


def token_case(
    case_id: str,
    question: str,
    input_text: str,
    filters: dict[str, Any],
    columns: list[str],
    expected_devices: list[str],
    expected_mcp: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "question": question,
        "input_text": input_text,
        "filters": filters,
        "columns": columns,
        "expected_devices": expected_devices,
        "expected_mcp": expected_mcp or [],
        "pandas_code": f"df = match_product_tokens({input_text!r}, sources['product_data'])\nresult = df[{columns!r}].reset_index(drop=True)",
    }


if __name__ == "__main__":
    raise SystemExit(main())
