from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "validation_outputs" / "data_analysis_business_validation_summary_20260729.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _filters_by_alias(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for job in plan.get("retrieval_jobs", []):
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "")
        result[alias] = job.get("filters") if isinstance(job.get("filters"), dict) else {}
    return result


def _operations(plan: dict[str, Any]) -> list[str]:
    return [
        str(step.get("operation") or "")
        for step in plan.get("pandas_execution_plan", [])
        if isinstance(step, dict)
    ]


def _aggregation_methods(plan: dict[str, Any]) -> list[str]:
    methods: list[str] = []
    for step in plan.get("pandas_execution_plan", []):
        if not isinstance(step, dict):
            continue
        method = str(step.get("agg_method") or "").strip().lower()
        if method:
            methods.append(method)
        for aggregation in step.get("aggregations", []):
            if not isinstance(aggregation, dict):
                continue
            method = str(
                aggregation.get("method") or aggregation.get("agg_method") or ""
            ).strip().lower()
            if method:
                methods.append(method)
    return methods


def _check(
    scenarios: list[dict[str, Any]],
    *,
    name: str,
    questions: list[str],
    checks: dict[str, bool],
    details: dict[str, Any],
) -> None:
    failed = [key for key, value in checks.items() if value is not True]
    scenarios.append(
        {
            "name": name,
            "questions": questions,
            "status": "ok" if not failed else "error",
            "checks": checks,
            "failed_checks": failed,
            "details": details,
        }
    )


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    single_q1 = _load(args.single_q1)
    single_batch = _load(args.single_batch)
    hold = _load(args.hold)
    regroup = _load(args.regroup)
    product_switch = _load(args.product_switch)
    equipment = _load(args.equipment)

    scenarios: list[dict[str, Any]] = []

    q1 = single_q1["results"][0]
    q1_plan = q1["intent"]["plan"]
    q1_filters = _filters_by_alias(q1_plan)
    q1_jobs = q1_plan.get("retrieval_jobs", [])
    q1_prod = next(
        (job for job in q1_jobs if job.get("dataset_key") == "production"),
        {},
    )
    q1_wip = next(
        (job for job in q1_jobs if str(job.get("dataset_key") or "").startswith("wip")),
        {},
    )
    q1_ordering = q1_plan.get("output_contract", {}).get("ordering", {})
    _check(
        scenarios,
        name="input_vs_process_wip_ranking",
        questions=[q1["question"]],
        checks={
            "pipeline_ok": q1.get("status") == "ok",
            "production_input_only": q1_prod.get("filters", {}).get("OPER_NAME")
            == {"operator": "eq", "value": "INPUT"},
            "wip_processes_only": set(
                q1_wip.get("filters", {})
                .get("OPER_NAME", {})
                .get("value", [])
            )
            == {"D/S1", "D/A1"},
            "wip_desc_without_arbitrary_limit": q1_ordering.get("sort_by") == "WIP"
            and q1_ordering.get("order") == "desc"
            and not q1_ordering.get("limit"),
            "no_presence_or_ratio_rewrite": "compare_presence"
            not in _operations(q1_plan)
            and not any(
                token in " ".join(q1["pandas"].get("columns", [])).upper()
                for token in ("RATIO", "RATE", "PERCENT")
            ),
            "pandas_ok_without_repair": q1["pandas"].get("status") == "ok"
            and q1["pandas"].get("repair_attempted") is False,
            "result_schema_and_rows": q1["pandas"].get("row_count", 0) > 0
            and {"PRODUCTION", "WIP"}.issubset(q1["pandas"].get("columns", [])),
        },
        details={
            "analysis_kind": q1_plan.get("analysis_kind"),
            "filters_by_alias": q1_filters,
            "ordering": q1_ordering,
            "row_count": q1["pandas"].get("row_count"),
            "columns": q1["pandas"].get("columns", []),
        },
    )

    singles = single_batch["results"]
    q2 = singles[1]
    q2_plan = q2["intent"]["plan"]
    q2_jobs = q2_plan.get("retrieval_jobs", [])
    prod_job = next(
        (job for job in q2_jobs if str(job.get("dataset_key") or "").startswith("production")),
        {},
    )
    wip_job = next(
        (job for job in q2_jobs if str(job.get("dataset_key") or "").startswith("wip")),
        {},
    )
    _check(
        scenarios,
        name="input_exists_da_wip_absent",
        questions=[q2["question"]],
        checks={
            "pipeline_ok": q2.get("status") == "ok",
            "source_specific_filters": prod_job.get("filters", {}).get("OPER_NAME")
            == {"operator": "eq", "value": "INPUT"}
            and set(
                wip_job.get("filters", {})
                .get("OPER_NAME", {})
                .get("value", [])
            )
            == {f"D/A{index}" for index in range(1, 7)},
            "presence_comparison_used": "compare_presence" in _operations(q2_plan),
            "contextual_metrics": q2_plan.get("output_contract", {}).get("column_labels")
            == {"INPUT_QTY": "INPUT 실적", "DA_WIP_QTY": "D/A 공정 WIP"},
            "pandas_ok_without_repair": q2["pandas"].get("status") == "ok"
            and q2["pandas"].get("repair_attempted") is False,
        },
        details={
            "row_count": q2["pandas"].get("row_count"),
            "columns": q2["pandas"].get("columns", []),
            "operations": _operations(q2_plan),
        },
    )

    q3 = singles[2]
    q3_plan = q3["intent"]["plan"]
    q3_job = q3_plan.get("retrieval_jobs", [{}])[0]
    _check(
        scenarios,
        name="numeric_tat_filter",
        questions=[q3["question"]],
        checks={
            "pipeline_ok": q3.get("status") == "ok",
            "numeric_ge_filter": q3_job.get("filters", {}).get("IN_TAT")
            == {"operator": "ge", "value": 10},
            "wb_process_expansion": set(
                q3_job.get("filters", {})
                .get("OPER_NAME", {})
                .get("value", [])
            )
            == {f"W/B{index}" for index in range(1, 7)},
            "pandas_ok_without_repair": q3["pandas"].get("status") == "ok"
            and q3["pandas"].get("repair_attempted") is False,
        },
        details={
            "row_count": q3["pandas"].get("row_count"),
            "columns": q3["pandas"].get("columns", []),
        },
    )

    for index, context_columns in (
        (3, {"EQUIP_MODEL", "RECIPE_ID", "OPER_NAME"}),
        (4, {"EQUIP_MODEL", "RECIPE_ID", "OPER_NAME"}),
        (5, {"EQUIP_MODEL", "RECIPE_ID", "OPER_NAME"}),
    ):
        item = singles[index]
        plan = item["intent"]["plan"]
        methods = _aggregation_methods(plan)
        _check(
            scenarios,
            name=f"uph_non_additive_{index - 2}",
            questions=[item["question"]],
            checks={
                "pipeline_ok": item.get("status") == "ok",
                "mean_used": "mean" in methods,
                "sum_not_used": "sum" not in methods,
                "context_columns_preserved": context_columns.issubset(
                    item["pandas"].get("columns", [])
                ),
                "pandas_ok_without_repair": item["pandas"].get("status") == "ok"
                and item["pandas"].get("repair_attempted") is False,
            },
            details={
                "aggregation_methods": methods,
                "row_count": item["pandas"].get("row_count"),
                "columns": item["pandas"].get("columns", []),
            },
        )

    hold_turn2 = hold["turns"][1]
    hold_binding = hold_turn2.get("upstream_parameter_binding", {})
    _check(
        scenarios,
        name="hold_lot_to_history_followup",
        questions=hold.get("questions", []),
        checks={
            "session_pipeline_ok": hold.get("status") == "ok",
            "followup_requery_selected": hold_turn2["intent"].get("request_scope")
            == "followup_requery"
            and hold_turn2["intent"].get("reference_mode") == "previous_result_rows",
            "lot_id_bound": any(
                binding.get("source_column") == "LOT_ID"
                and binding.get("target_param") == "LOT_ID"
                for binding in hold_binding.get("bindings", [])
            ),
            "one_history_row": hold_turn2["pandas"].get("row_count") == 1,
            "session_and_results_stored": hold.get("mongodb_verification", {}).get(
                "result_document_count"
            )
            == 2,
        },
        details={
            "binding": hold_binding,
            "columns": hold_turn2["pandas"].get("columns", []),
        },
    )

    regroup_turn2 = regroup["turns"][1]
    regroup_filters = (
        regroup_turn2["intent"]
        .get("condition_resolution", {})
        .get("effective_filters", {})
    )
    regroup_values = []
    for source in regroup_filters.values():
        values = (
            source.get("filters", {})
            .get("OPER_NAME", {})
            .get("value", [])
        )
        regroup_values.append(set(values))
    _check(
        scenarios,
        name="process_filter_inheritance_and_regroup",
        questions=regroup.get("questions", []),
        checks={
            "session_pipeline_ok": regroup.get("status") == "ok",
            "previous_source_transform": regroup_turn2["intent"].get("request_scope")
            == "followup_transform"
            and regroup_turn2["intent"].get("reference_mode") == "previous_source",
            "no_new_retrieval": not regroup_turn2["intent"].get("retrieval_jobs"),
            "wb_filters_kept_on_both_sources": len(regroup_values) == 2
            and all(
                values == {f"W/B{index}" for index in range(1, 7)}
                for values in regroup_values
            ),
            "product_grain_without_process_column": regroup_turn2["pandas"].get(
                "row_count"
            )
            == 3
            and "OPER_NAME" not in regroup_turn2["pandas"].get("columns", []),
        },
        details={
            "effective_filters": regroup_filters,
            "columns": regroup_turn2["pandas"].get("columns", []),
        },
    )

    switch_turn1, switch_turn2 = product_switch["turns"]
    mobile_filter = (
        switch_turn1["intent"]
        .get("retrieval_jobs", [{}])[0]
        .get("filters", {})
        .get("MCP_NO", {})
    )
    pop_filter = (
        switch_turn2["intent"]
        .get("condition_resolution", {})
        .get("effective_filters", {})
        .get("production_yesterday", {})
        .get("filters", {})
        .get("MCP_NO", {})
    )
    _check(
        scenarios,
        name="mobile_to_pop_filter_switch",
        questions=product_switch.get("questions", []),
        checks={
            "session_pipeline_ok": product_switch.get("status") == "ok",
            "mobile_null_or_empty": mobile_filter.get("operator") == "null_or_empty",
            "previous_source_transform": switch_turn2["intent"].get("request_scope")
            == "followup_transform"
            and switch_turn2["intent"].get("reference_mode") == "previous_source",
            "pop_not_blank": pop_filter.get("operator") == "not_blank",
            "no_new_retrieval": not switch_turn2["intent"].get("retrieval_jobs"),
            "result_rows_present": switch_turn2["pandas"].get("row_count") == 2,
        },
        details={
            "mobile_mcp_filter": mobile_filter,
            "pop_mcp_filter": pop_filter,
            "columns": switch_turn2["pandas"].get("columns", []),
        },
    )

    equipment_turns = equipment["turns"]
    expected_scopes = [
        ("new_analysis", "none"),
        ("followup_requery", "previous_result_rows"),
        ("followup_transform", "previous_result_transform"),
        ("new_analysis", "none"),
    ]
    actual_scopes = [
        (
            turn["intent"].get("request_scope"),
            turn["intent"].get("reference_mode"),
        )
        for turn in equipment_turns
    ]
    _check(
        scenarios,
        name="product_equipment_transform_and_independent_reset",
        questions=equipment.get("questions", []),
        checks={
            "session_pipeline_ok": equipment.get("status") == "ok",
            "scope_sequence": actual_scopes == expected_scopes,
            "equipment_count_and_list": {"EQUIP_COUNT", "EQUIP_LIST"}.issubset(
                equipment_turns[1]["pandas"].get("columns", [])
            )
            and equipment_turns[1]["pandas"].get("row_count") == 3,
            "top_transform_one_row": equipment_turns[2]["pandas"].get("row_count")
            == 1
            and not equipment_turns[2]["intent"].get("retrieval_jobs"),
            "independent_wb_retrieval": equipment_turns[3]["intent"].get(
                "request_scope"
            )
            == "new_analysis"
            and equipment_turns[3]["intent"].get("reference_mode") == "none",
            "session_and_results_stored": equipment.get(
                "mongodb_verification", {}
            ).get("result_document_count")
            == 4,
        },
        details={
            "scope_sequence": actual_scopes,
            "row_counts": [
                turn["pandas"].get("row_count") for turn in equipment_turns
            ],
            "turn2_columns": equipment_turns[1]["pandas"].get("columns", []),
        },
    )

    failures = [
        {
            "scenario": scenario["name"],
            "failed_checks": scenario["failed_checks"],
        }
        for scenario in scenarios
        if scenario["status"] != "ok"
    ]
    return {
        "status": "ok" if not failures else "error",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "reference_date": single_q1.get("environment", {}).get("reference_date"),
            "llm_provider": single_q1.get("environment", {}).get("llm_provider"),
            "llm_model": single_q1.get("environment", {}).get("llm_model"),
            "retrieval_mode": single_q1.get("environment", {}).get("retrieval_mode"),
            "session_state": "real_mongodb",
        },
        "summary": {
            "scenario_count": len(scenarios),
            "passed": len(scenarios) - len(failures),
            "failed": len(failures),
            "single_turn_questions": 6,
            "multiturn_scenarios": 4,
            "multiturn_turns": 10,
        },
        "stability_observations": [
            {
                "question": singles[0]["question"],
                "initial_batch_status": singles[0].get("status"),
                "initial_error": singles[0].get("errors", []),
                "same_configuration_rerun_status": q1.get("status"),
                "interpretation": (
                    "Gemini가 한 번 code 없는 응답을 반환했으나 동일 최신 구성 재실행은 "
                    "정상 코드를 생성했습니다. missing_code repair 프롬프트에는 기존 코드가 "
                    "없어도 intent/schema로 전체 코드를 재생성하도록 보강했습니다."
                ),
            }
        ],
        "scenarios": scenarios,
        "failures": failures,
        "source_reports": {
            "single_q1": str(args.single_q1.relative_to(ROOT)),
            "single_batch": str(args.single_batch.relative_to(ROOT)),
            "hold": str(args.hold.relative_to(ROOT)),
            "regroup": str(args.regroup.relative_to(ROOT)),
            "product_switch": str(args.product_switch.relative_to(ROOT)),
            "equipment": str(args.equipment.relative_to(ROOT)),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실무형 Data Analysis 단일/후속 검증 보고서를 의미 계약 기준으로 통합합니다."
    )
    output_dir = ROOT / "validation_outputs"
    parser.add_argument(
        "--single-q1",
        type=Path,
        default=output_dir / "business_q1_raw_diagnostic_20260729.json",
    )
    parser.add_argument(
        "--single-batch",
        type=Path,
        default=output_dir / "data_analysis_business_singleturn_live_20260729_final.json",
    )
    parser.add_argument(
        "--hold",
        type=Path,
        default=output_dir / "business_followup_hold_live_20260729_rerun.json",
    )
    parser.add_argument(
        "--regroup",
        type=Path,
        default=output_dir / "business_followup_regroup_live_20260729.json",
    )
    parser.add_argument(
        "--product-switch",
        type=Path,
        default=output_dir
        / "business_followup_product_switch_live_20260729_rerun.json",
    )
    parser.add_argument(
        "--equipment",
        type=Path,
        default=output_dir / "business_followup_equipment_live_20260729_rerun3.json",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = summarize(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output),
                **report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
