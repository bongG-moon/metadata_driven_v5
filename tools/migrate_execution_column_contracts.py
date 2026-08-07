from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

try:
    from .replace_process_group_domains import load_dotenv
except ImportError:
    from replace_process_group_domains import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DATABASE = "datagov"
TABLE_COLLECTION = "agent_v4_table_catalog_items"
DOMAIN_COLLECTION = "agent_v4_domain_items"
TABLE_KEYS = ("eqp_uph", "equipment_assign", "target")
DOMAIN_IDS = (
    "domain:analysis_recipes:equipment_assignment_uph_join",
    "domain:quantity_terms:target_data",
)


def _load_validators() -> tuple[Any, Any]:
    sys.path.insert(0, str(ROOT))
    from tools import validate_representative_questions as harness

    harness.install_lfx_stubs()
    table_writer = harness.load_module(
        ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py"
    )
    domain_writer = harness.load_module(
        ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py"
    )
    return table_writer, domain_writer


def _column_names(columns: Any) -> list[str]:
    result: list[str] = []
    for item in columns if isinstance(columns, list) else []:
        value = item.get("column_name") if isinstance(item, dict) else item
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _set_columns(payload: dict[str, Any], names: list[str]) -> None:
    existing = payload.get("columns") if isinstance(payload.get("columns"), list) else []
    structured = any(isinstance(item, dict) for item in existing)
    payload["columns"] = (
        [{"column_name": name} for name in names]
        if structured
        else names
    )


def _migrate_table(doc: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(doc)
    key = str(item.get("dataset_key") or "")
    payload = item.setdefault("payload", {})
    if key == "eqp_uph":
        _set_columns(payload, [
            "EQUIP_MODEL", "OPER", "OPER_NAME", "PRESS_CNT", "MODE", "TECH", "ORG",
            "DENSITY", "PKG1", "PKG2", "LEAD", "MCP_NO", "RECIPE_ID", "UPH", "LOAD_DT", "BASE_DT",
        ])
        payload["filter_mappings"] = {
            "EQP_MODEL": ["EQUIP_MODEL"], "OPER_NAME": ["OPER_NAME"], "OPER_NUM": ["OPER"],
            "MODE": ["MODE"], "TECH": ["TECH"], "ORG": ["ORG"], "DEN": ["DENSITY"],
            "PKG_TYPE1": ["PKG1"], "PKG_TYPE2": ["PKG2"], "LEAD": ["LEAD"],
            "MCP_NO": ["MCP_NO"], "RECIPE_ID": ["RECIPE_ID"], "UPH": ["UPH"],
        }
        payload.pop("standard_column_aliases", None)
        payload["default_detail_columns"] = ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
    elif key == "equipment_assign":
        payload["filter_mappings"] = {
            "EQP_ID": ["EQUIP_ID"], "EQP_MODEL": ["EQUIP_MODEL"], "MODE": ["MODE"],
            "DEN": ["DENSITY"], "TECH": ["TECH"], "PKG_TYPE1": ["PKG1"],
            "PKG_TYPE2": ["PKG2"], "LEAD": ["LEAD"], "MCP_NO": ["MCP_NO"],
            "ORG": ["ORG"], "DEVICE": ["DEVICE"], "DEVICE_DESC": ["DEVICE_DESC"],
            "OPER_NUM": ["OPER"], "OPER_NAME": ["OPER_NM"], "LOT_ID": ["LOT_ID"],
            "RECIPE_ID": ["RECIPE_ID"],
        }
        payload.pop("standard_column_aliases", None)
        semantics = payload.get("metric_semantics") if isinstance(payload.get("metric_semantics"), dict) else {}
        if "EQUIP_ID" in semantics and "EQP_ID" not in semantics:
            semantics["EQP_ID"] = semantics.pop("EQUIP_ID")
        payload["metric_semantics"] = semantics
        payload["default_detail_columns"] = ["EQP_ID"]
    elif key == "target":
        payload["filter_mappings"] = {
            "DATE": ["DATE"], "MODE": ["Mode"], "DEN": ["DEN"], "TECH": ["TECH"],
            "ORG": ["ORG"], "PKG_TYPE1": ["PKG1"], "PKG_TYPE2": ["PKG2"],
            "LEAD": ["LEAD"], "MCP_NO": ["MCP NO"],
            "INPUT_PLAN_QTY": ["INPUT 계획"], "OUT_PLAN_QTY": ["OUT 계획"],
        }
        payload.pop("standard_column_aliases", None)
        semantics = payload.get("metric_semantics") if isinstance(payload.get("metric_semantics"), dict) else {}
        if "INPUT 계획" in semantics:
            semantics["INPUT_PLAN_QTY"] = semantics.pop("INPUT 계획")
        if "OUT 계획" in semantics:
            semantics["OUT_PLAN_QTY"] = semantics.pop("OUT 계획")
        payload["metric_semantics"] = semantics
    item["updated_at"] = datetime.now(timezone.utc)
    return item


def _migrate_domain(doc: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(doc)
    payload = item.setdefault("payload", {})
    logical_key = f"{item.get('section')}:{item.get('key')}"
    if logical_key == "analysis_recipes:equipment_assignment_uph_join":
        payload.pop("left_key_mappings", None)
        payload.pop("right_key_mappings", None)
        payload["join_keys"] = ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
        payload["selection_criteria"] = {
            "required_any_aliases": [
                "배정 장비",
                "할당 장비",
                "배정된 장비",
                "장비 대수",
                "장비 목록",
                "장비 LIST",
                "현재 장비",
            ],
            "source_datasets": ["equipment_assign", "eqp_uph"],
        }
    elif logical_key == "quantity_terms:target_data":
        payload["data_source"] = "target"
        payload["metric_columns"] = ["INPUT_PLAN_QTY", "OUT_PLAN_QTY"]
        payload["selection_criteria"] = [
            "생산계획 또는 계획정보처럼 INPUT/OUT을 한정하지 않으면 두 metric을 각각 집계해 함께 표시한다.",
            "투입계획은 INPUT_PLAN_QTY, OUT계획·TARGET·생산목표는 OUT_PLAN_QTY를 사용한다.",
            "Table Catalog에 없는 PLAN_QTY 같은 포괄 실행 컬럼을 만들지 않는다.",
        ]
        payload["usage_condition"] = "질문에 계획 또는 스케쥴 의미가 포함된 경우에만 사용"
    item["updated_at"] = datetime.now(timezone.utc)
    return item


def _table_validation_errors(writer: Any, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [
        {"dataset_key": doc.get("dataset_key"), "status": doc.get("status", "active"), "payload": deepcopy(doc.get("payload") or {})}
        for doc in docs
    ]
    return writer._deterministic_errors({"items": items, "errors": []})


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate runtime column contracts without an external LLM.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", default="validation_outputs/execution_column_contract_migration.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is required")
    database_name = os.getenv("MONGODB_DATABASE", DATABASE).strip() or DATABASE
    table_name = os.getenv("MONGODB_TABLE_CATALOG_COLLECTION", TABLE_COLLECTION).strip() or TABLE_COLLECTION
    domain_name = os.getenv("MONGODB_DOMAIN_COLLECTION", DOMAIN_COLLECTION).strip() or DOMAIN_COLLECTION
    table_writer, domain_writer = _load_validators()
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        database = client[database_name]
        tables = list(database[table_name].find({"dataset_key": {"$in": list(TABLE_KEYS)}}))
        domains = list(database[domain_name].find({"_id": {"$in": list(DOMAIN_IDS)}}))
        if len(tables) != len(TABLE_KEYS) or len(domains) != len(DOMAIN_IDS):
            raise RuntimeError(f"migration targets missing: tables={len(tables)}, domains={len(domains)}")
        migrated_tables = [_migrate_table(doc) for doc in tables]
        migrated_domains = [_migrate_domain(doc) for doc in domains]
        errors = _table_validation_errors(table_writer, migrated_tables)
        domain_payload = {
            "items": [
                {"section": doc.get("section"), "key": doc.get("key"), "status": doc.get("status", "active"), "payload": deepcopy(doc.get("payload") or {})}
                for doc in migrated_domains
            ],
            "errors": [],
        }
        if hasattr(domain_writer, "_deterministic_errors"):
            errors.extend(domain_writer._deterministic_errors(domain_payload))
        if errors:
            raise RuntimeError(f"deterministic validation failed: {errors}")
        applied = False
        if args.apply:
            try:
                for doc in migrated_tables:
                    database[table_name].replace_one({"_id": doc["_id"]}, doc, upsert=False)
                for doc in migrated_domains:
                    database[domain_name].replace_one({"_id": doc["_id"]}, doc, upsert=False)
                applied = True
            except Exception:
                for doc in tables:
                    database[table_name].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                for doc in domains:
                    database[domain_name].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                raise
        report = {
            "status": "applied" if applied else "dry_run_ok",
            "database": database_name,
            "table_dataset_keys": sorted(TABLE_KEYS),
            "domain_ids": sorted(DOMAIN_IDS),
            "validation_errors": [],
            "before_after": {
                doc["dataset_key"]: {
                    "before_columns": _column_names(next(old for old in tables if old["dataset_key"] == doc["dataset_key"])["payload"].get("columns")),
                    "after_columns": _column_names(doc["payload"].get("columns")),
                    "after_filter_mappings": doc["payload"].get("filter_mappings"),
                    "after_metric_semantics": doc["payload"].get("metric_semantics"),
                }
                for doc in migrated_tables
            },
        }
        output = Path(args.output)
        if not output.is_absolute():
            output = ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
