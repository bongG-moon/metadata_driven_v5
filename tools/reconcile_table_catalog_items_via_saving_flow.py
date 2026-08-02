from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .reconcile_domain_items_via_saving_flow import (
        call_google_model,
        resolve_llm_config,
    )
    from .replace_process_group_domains import load_dotenv
except ImportError:
    from reconcile_domain_items_via_saving_flow import (
        call_google_model,
        resolve_llm_config,
    )
    from replace_process_group_domains import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data_catalog.txt"
FLOW_DIR = ROOT / "langflow_components" / "table_catalog_saving_flow"
PROMPT_PATH = FLOW_DIR / "03_saving_prompt_template_ko.md"
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_table_catalog_items"

TARGET_SPECS = (
    {
        "dataset_key": "production_today",
        "start": "당일용 생산 실적 데이터는 production_today로 등록해줘.",
        "end": "Production History",
    },
    {
        "dataset_key": "production",
        "start": "Production History",
        "end": "<!-- single_wip_today:start -->",
    },
    {
        "dataset_key": "equipment_assign",
        "start": "Equipment Status",
        "end": "Equipment Recipe UPH",
    },
    {
        "dataset_key": "eqp_uph",
        "start": "Equipment Recipe UPH",
        "end": "LOT Status",
    },
    {
        "dataset_key": "lot_status",
        "start": "LOT Status",
        "end": "<!-- single_hold_history:start -->",
    },
    {
        "dataset_key": "hold_history",
        "start": "<!-- single_hold_history:start -->",
        "end": None,
    },
)


def _extract_block(text: str, start: str, end: str | None) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index) if end else len(text)
    return text[start_index:end_index].strip()


def build_registration_requests(text: str) -> list[dict[str, str]]:
    return [
        {
            "dataset_key": str(spec["dataset_key"]),
            "raw_text": _extract_block(
                text,
                str(spec["start"]),
                str(spec["end"]) if spec["end"] else None,
            ),
        }
        for spec in TARGET_SPECS
    ]


def _load_components() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from tools import validate_representative_questions as harness

    harness.install_lfx_stubs()
    return {
        "request": harness.load_module(
            FLOW_DIR / "00_table_catalog_saving_request_loader.py"
        ),
        "variables": harness.load_module(
            FLOW_DIR / "03_table_catalog_saving_variables_builder.py"
        ),
        "normalizer": harness.load_module(
            FLOW_DIR / "04_table_catalog_saving_result_normalizer.py"
        ),
        "similarity": harness.load_module(
            FLOW_DIR / "05_table_catalog_similarity_checker.py"
        ),
        "writer": harness.load_module(
            FLOW_DIR / "07_table_catalog_review_writer.py"
        ),
    }


def _semantic_errors(dataset_key: str, items: list[Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    generated = [
        item for item in items
        if isinstance(item, dict) and str(item.get("dataset_key") or "") == dataset_key
    ]
    if len(generated) != 1 or len(items) != 1:
        return [{
            "type": "dataset_key_mismatch",
            "message": f"{dataset_key} 단일 item이 생성되지 않았습니다.",
        }]

    payload = generated[0].get("payload")
    payload = payload if isinstance(payload, dict) else {}
    source_config = payload.get("source_config")
    source_config = source_config if isinstance(source_config, dict) else {}

    if dataset_key in {"production_today", "production"}:
        criteria = payload.get("selection_criteria")
        criteria = criteria if isinstance(criteria, dict) else {}
        expected_scope = "current_day" if dataset_key == "production_today" else "history"
        if criteria.get("time_scope") != expected_scope:
            errors.append({
                "type": "selection_time_scope_mismatch",
                "message": f"{dataset_key} selection_criteria.time_scope must be {expected_scope}.",
            })
        columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
        column_names = [
            str(item.get("column_name") or "") if isinstance(item, dict) else str(item or "")
            for item in columns
        ]
        if "PRODUCTION" not in column_names:
            errors.append({
                "type": "production_metric_column_missing",
                "message": f"{dataset_key} must declare the PRODUCTION source column.",
            })
    elif dataset_key == "equipment_assign":
        filter_mappings = payload.get("filter_mappings")
        filter_mappings = filter_mappings if isinstance(filter_mappings, dict) else {}
        model_candidates = filter_mappings.get("EQP_MODEL")
        model_candidates = (
            model_candidates
            if isinstance(model_candidates, list)
            else [model_candidates] if model_candidates else []
        )
        if "EQUIP_MODEL" not in model_candidates:
            errors.append({
                "type": "equipment_model_mapping_mismatch",
                "message": "equipment_assign must map standard EQP_MODEL to source EQUIP_MODEL.",
            })
        if "EQPIP_MODEL" in json.dumps(payload, ensure_ascii=False):
            errors.append({
                "type": "equipment_model_typo_present",
                "message": "equipment_assign contains the obsolete EQPIP_MODEL typo.",
            })
    elif dataset_key == "eqp_uph":
        expected_details = ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
        if payload.get("default_detail_columns") != expected_details:
            errors.append({
                "type": "default_detail_columns_mismatch",
                "message": "eqp_uph 기본 상세 컬럼이 원문과 다릅니다.",
            })
        semantics = payload.get("metric_semantics")
        semantics = semantics if isinstance(semantics, dict) else {}
        uph = semantics.get("UPH")
        uph = uph if isinstance(uph, dict) else {}
        if (
            uph.get("semantic_type") != "average"
            or uph.get("additive") is not False
            or uph.get("default_rollup") != "mean"
            or uph.get("allowed_rollups") != ["mean"]
            or uph.get("source_already_aggregated") is not True
        ):
            errors.append({
                "type": "uph_metric_semantics_mismatch",
                "message": "UPH 비가산 평균 계약이 원문과 다릅니다.",
            })
    elif dataset_key == "lot_status":
        expected_details = [
            "LOT_ID",
            "OPER_NAME",
            "PROD_QTY",
            "WF_QTY",
            "IN_TAT",
            "CUM_TAT",
            "HOLD_STAT",
            "HOLD_REASON",
            "LOT_STAT",
        ]
        if payload.get("default_detail_columns") != expected_details:
            errors.append({
                "type": "default_detail_columns_mismatch",
                "message": "lot_status 기본 상세 컬럼이 원문과 다릅니다.",
            })
    elif dataset_key == "hold_history":
        expected_details = ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"]
        if payload.get("default_detail_columns") != expected_details:
            errors.append({
                "type": "default_detail_columns_mismatch",
                "message": "hold_history 기본 상세 컬럼이 원문과 다릅니다.",
            })
        if payload.get("required_params") != ["LOT_ID"]:
            errors.append({
                "type": "required_params_mismatch",
                "message": "hold_history 필수 파라미터가 LOT_ID 단일 항목이 아닙니다.",
            })
        expected_binding = {
            "entity_type": "lot",
            "source_alias": "previous_result",
            "source_column": "LOT_ID",
            "target_param": "LOT_ID",
            "operator": "in",
            "max_values": 200,
        }
        if source_config.get("upstream_bindings") != [expected_binding]:
            errors.append({
                "type": "upstream_bindings_mismatch",
                "message": "hold_history의 previous_result LOT_ID 바인딩이 원문과 다릅니다.",
            })
        query = str(source_config.get("query_template") or "")
        if "WHERE LOT_ID IN ({LOT_ID})" not in query:
            errors.append({
                "type": "query_template_binding_missing",
                "message": "hold_history SQL에 LOT_ID IN placeholder가 없습니다.",
            })
    return errors


def generate_and_validate(
    requests: list[dict[str, str]],
    *,
    components: dict[str, Any],
    llm_config: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for request_spec in requests:
        payload = components["request"].build_request(
            request_spec["raw_text"],
            duplicate_action="replace",
            dry_run=True,
        )
        variables = components["variables"].build_variables(payload)
        llm_response = call_google_model(
            prompt_template.format(**variables),
            llm_config,
        )
        payload = components["normalizer"].normalize_authoring(
            payload,
            llm_response,
        )
        semantic_errors = _semantic_errors(
            request_spec["dataset_key"],
            payload.get("items") or [],
        )
        if not semantic_errors and not payload.get("errors"):
            payload = components["similarity"].check_similarity(
                payload,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            payload = components["writer"].review_and_write(
                payload,
                "",
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
        write_result = payload.get("write_result")
        write_result = write_result if isinstance(write_result, dict) else {}
        success = (
            not semantic_errors
            and not payload.get("errors")
            and bool(write_result.get("success"))
        )
        results.append({
            "dataset_key": request_spec["dataset_key"],
            "success": success,
            "semantic_errors": semantic_errors,
            "normalization_errors": deepcopy(payload.get("errors") or []),
            "dry_run_result": deepcopy(write_result),
        })
        if success:
            prepared.append({
                **request_spec,
                "llm_response": llm_response,
            })
    return prepared, results


def apply_replacement(
    prepared: list[dict[str, Any]],
    *,
    components: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> dict[str, Any]:
    from pymongo import MongoClient

    ids = [f"table_catalog:{item['dataset_key']}" for item in prepared]
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    collection = client[mongo_database][collection_name]
    backups = {
        doc_id: collection.find_one({"_id": doc_id})
        for doc_id in ids
    }
    writes: list[dict[str, Any]] = []
    try:
        for prepared_item in prepared:
            payload = components["request"].build_request(
                prepared_item["raw_text"],
                duplicate_action="replace",
                dry_run=False,
            )
            payload = components["normalizer"].normalize_authoring(
                payload,
                prepared_item["llm_response"],
            )
            payload = components["similarity"].check_similarity(
                payload,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            payload = components["writer"].review_and_write(
                payload,
                "",
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            write_result = payload.get("write_result")
            write_result = write_result if isinstance(write_result, dict) else {}
            writes.append({
                "dataset_key": prepared_item["dataset_key"],
                "success": bool(write_result.get("success")),
                "operation_by_key": deepcopy(write_result.get("operation_by_key") or []),
                "errors": deepcopy(payload.get("errors") or write_result.get("errors") or []),
            })
            if not write_result.get("success") or payload.get("errors"):
                raise RuntimeError(
                    f"Table Catalog Saving live write failed: {prepared_item['dataset_key']}"
                )

        saved_items = list(collection.find(
            {"_id": {"$in": ids}},
            {
                "_id": 1,
                "dataset_key": 1,
                "payload.default_detail_columns": 1,
                "payload.filter_mappings": 1,
                "payload.columns": 1,
                "payload.selection_criteria": 1,
                "payload.metric_semantics": 1,
                "payload.required_params": 1,
                "payload.source_config.upstream_bindings": 1,
                "payload.source_config.query_template": 1,
            },
        ))
        verification_errors: list[dict[str, str]] = []
        for item in saved_items:
            verification_errors.extend(
                _semantic_errors(str(item.get("dataset_key") or ""), [item])
            )
        if len(saved_items) != len(ids) or verification_errors:
            raise RuntimeError(
                "post-apply verification failed: "
                f"count={len(saved_items)}, errors={verification_errors}"
            )
        return {
            "success": True,
            "writes": writes,
            "verified_dataset_keys": sorted(
                str(item.get("dataset_key") or "") for item in saved_items
            ),
        }
    except Exception:
        for doc_id in ids:
            collection.delete_one({"_id": doc_id})
            backup = backups.get(doc_id)
            if backup:
                collection.replace_one({"_id": doc_id}, backup, upsert=True)
        raise
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "실제 Table Catalog Saving 컴포넌트 순서로 eqp_uph, lot_status, "
            "hold_history 항목을 검증하고 선택적으로 replace 저장합니다."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="모든 dry-run 검증이 성공한 경우에만 세 항목을 replace 저장합니다.",
    )
    parser.add_argument(
        "--output",
        default="validation_outputs/table_catalog_reconciliation_20260729.json",
    )
    parser.add_argument(
        "--dataset-key",
        action="append",
        default=[],
        help="Reconcile only selected dataset keys. Repeat this option for multiple datasets.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI가 필요합니다.")
    mongo_database = os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection_name = (
        os.getenv("MONGODB_TABLE_CATALOG_COLLECTION", DEFAULT_COLLECTION).strip()
        or DEFAULT_COLLECTION
    )
    llm_config = resolve_llm_config()
    components = _load_components()
    requests = build_registration_requests(CATALOG_PATH.read_text(encoding="utf-8"))
    if args.dataset_key:
        selected = {str(value).strip() for value in args.dataset_key if str(value).strip()}
        requests = [item for item in requests if item["dataset_key"] in selected]
        missing = sorted(selected - {item["dataset_key"] for item in requests})
        if missing:
            raise RuntimeError(f"Unknown dataset keys: {', '.join(missing)}")
    prepared, dry_run_results = generate_and_validate(
        requests,
        components=components,
        llm_config=llm_config,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=collection_name,
    )
    dry_run_success = (
        len(prepared) == len(requests)
        and all(item.get("success") for item in dry_run_results)
    )
    apply_result: dict[str, Any] = {}
    status = "dry_run_ok" if dry_run_success else "dry_run_error"
    if args.apply:
        if not dry_run_success:
            raise RuntimeError("Dry-run 검증이 모두 성공하지 않아 실제 저장을 중단했습니다.")
        apply_result = apply_replacement(
            prepared,
            components=components,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_name=collection_name,
        )
        status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "database": mongo_database,
        "collection_name": collection_name,
        "llm": {
            "provider": "Google Generative AI",
            "model": llm_config["model"],
        },
        "request_count": len(requests),
        "prepared_count": len(prepared),
        "dry_run_results": dry_run_results,
        "apply_result": apply_result,
    }
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": status,
        "output": str(output_path),
        "prepared_count": len(prepared),
        "apply_result": apply_result,
    }, ensure_ascii=False, indent=2))
    return 0 if status in {"dry_run_ok", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
