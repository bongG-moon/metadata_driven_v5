from __future__ import annotations

import argparse
import os
from importlib import import_module
from pathlib import Path
from typing import Any

from bson import json_util


EXPORT_FORMAT = "metadata_driven_v5.mongodb_metadata_bundle.v1"
DEFAULT_DATABASE = "datagov"
DEFAULT_EXPORT_DIR = Path("metadata_exports")


class MetadataTarget:
    def __init__(self, key: str, env_name: str, default_collection: str):
        self.key = key
        self.env_name = env_name
        self.default_collection = default_collection


# python tools\upload_json_to_mongodb.py `
#   --input metadata_exports\mongodb_metadata_export_20260701_130231.json `
#   --mode upsert



METADATA_TARGETS: dict[str, MetadataTarget] = {
    "domain": MetadataTarget("domain", "MONGODB_DOMAIN_COLLECTION", "agent_v4_domain_items"),
    "table-catalog": MetadataTarget("table-catalog", "MONGODB_TABLE_CATALOG_COLLECTION", "agent_v4_table_catalog_items"),
    "main-flow-filter": MetadataTarget("main-flow-filter", "MONGODB_MAIN_FLOW_FILTER_COLLECTION", "agent_v4_main_flow_filters"),
}

KIND_ALIASES = {
    "domain": "domain",
    "domain-items": "domain",
    "domain_items": "domain",
    "table": "table-catalog",
    "table-catalog": "table-catalog",
    "table_catalog": "table-catalog",
    "table-catalog-items": "table-catalog",
    "table_catalog_items": "table-catalog",
    "main": "main-flow-filter",
    "main-variable": "main-flow-filter",
    "main_variable": "main-flow-filter",
    "main-flow-filter": "main-flow-filter",
    "main_flow_filter": "main-flow-filter",
    "main-flow-filters": "main-flow-filter",
    "main_flow_filters": "main-flow-filter",
}


class MongoUploadConfig:
    def __init__(self, mongo_uri: str, database: str, collections: dict[str, str], timeout_ms: int = 5000):
        self.mongo_uri = mongo_uri
        self.database = database
        self.collections = collections
        self.timeout_ms = timeout_ms


def load_dotenv(env_file: str | Path = ".env") -> None:
    path = Path(env_file)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_metadata_kinds(values: list[str] | None) -> list[str]:
    if not values:
        return []
    kinds: list[str] = []
    for value in values:
        for raw_part in str(value).split(","):
            part = raw_part.strip()
            if not part:
                continue
            canonical = KIND_ALIASES.get(part.lower())
            if canonical is None:
                allowed = ", ".join(sorted(KIND_ALIASES))
                raise ValueError(f"Unknown metadata kind: {part}. Allowed values: {allowed}")
            if canonical not in kinds:
                kinds.append(canonical)
    return kinds


def resolve_config(args: argparse.Namespace) -> MongoUploadConfig:
    collections = {
        "domain": args.domain_collection or os.getenv("MONGODB_DOMAIN_COLLECTION", METADATA_TARGETS["domain"].default_collection),
        "table-catalog": args.table_catalog_collection or os.getenv("MONGODB_TABLE_CATALOG_COLLECTION", METADATA_TARGETS["table-catalog"].default_collection),
        "main-flow-filter": args.main_flow_filter_collection or os.getenv("MONGODB_MAIN_FLOW_FILTER_COLLECTION", METADATA_TARGETS["main-flow-filter"].default_collection),
    }
    return MongoUploadConfig(
        mongo_uri=args.mongo_uri or os.getenv("MONGODB_URI", ""),
        database=args.database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collections=collections,
        timeout_ms=int(args.timeout_ms),
    )


def latest_export_path() -> Path:
    candidates = sorted(DEFAULT_EXPORT_DIR.glob("mongodb_metadata_export_*.json"))
    if not candidates:
        raise FileNotFoundError("No metadata export JSON found under metadata_exports/. Use --input.")
    return candidates[-1]


def load_bundle(input_path: str | Path) -> dict[str, Any]:
    path = Path(input_path)
    bundle = json_util.loads(path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or "collections" not in bundle:
        raise ValueError("Input JSON must be an export bundle made by export_mongodb_metadata_to_json.py.")
    return bundle


def selected_bundle_kinds(bundle: dict[str, Any], requested_kinds: list[str]) -> list[str]:
    bundle_kinds = [kind for kind in bundle.get("collections", {}) if kind in METADATA_TARGETS]
    if not requested_kinds:
        return bundle_kinds
    missing = [kind for kind in requested_kinds if kind not in bundle.get("collections", {})]
    if missing:
        raise ValueError(f"Requested metadata kinds are not in bundle: {', '.join(missing)}")
    return requested_kinds


def upload_bundle(
    input_path: str | Path,
    config: MongoUploadConfig,
    selected_kinds: list[str],
    mode: str = "upsert",
    dry_run: bool = False,
) -> dict[str, Any]:
    bundle = load_bundle(input_path)
    kinds = selected_bundle_kinds(bundle, selected_kinds)
    summary: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "mode": mode,
        "input_path": str(Path(input_path)),
        "database": config.database,
        "collections": {},
    }

    if dry_run:
        for kind in kinds:
            docs = list(bundle["collections"][kind].get("documents", []))
            summary["collections"][kind] = {
                "collection_name": config.collections[kind],
                "source_collection_name": bundle["collections"][kind].get("collection_name", ""),
                "document_count": len(docs),
                "written_count": 0,
            }
        return summary

    if not config.mongo_uri:
        raise RuntimeError("MONGODB_URI is required for MongoDB upload. Use --dry-run to inspect without writing.")

    pymongo = import_module("pymongo")
    client = pymongo.MongoClient(config.mongo_uri, serverSelectionTimeoutMS=config.timeout_ms)
    try:
        database = client[config.database]
        for kind in kinds:
            docs = list(bundle["collections"][kind].get("documents", []))
            collection_name = config.collections[kind]
            collection = database[collection_name]
            written_count = 0
            if mode == "replace":
                collection.delete_many({})
                if docs:
                    collection.insert_many(docs)
                written_count = len(docs)
            else:
                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    doc_id = doc.get("_id")
                    if doc_id is None:
                        collection.insert_one(doc)
                    else:
                        collection.replace_one({"_id": doc_id}, doc, upsert=True)
                    written_count += 1
            summary["collections"][kind] = {
                "collection_name": collection_name,
                "source_collection_name": bundle["collections"][kind].get("collection_name", ""),
                "document_count": len(docs),
                "written_count": written_count,
            }
        return summary
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload a metadata-driven v5 MongoDB metadata JSON bundle to MongoDB.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file. Defaults to .env.")
    parser.add_argument("--input", default="", help="Export JSON path. Defaults to latest metadata_exports/mongodb_metadata_export_*.json.")
    parser.add_argument("--mongo-uri", default="", help="MongoDB URI. Defaults to MONGODB_URI.")
    parser.add_argument("--database", default="", help="Target MongoDB database. Defaults to MONGODB_DATABASE or datagov.")
    parser.add_argument("--domain-collection", default="", help="Target domain metadata collection name.")
    parser.add_argument("--table-catalog-collection", default="", help="Target table catalog metadata collection name.")
    parser.add_argument("--main-flow-filter-collection", default="", help="Target main variable/filter metadata collection name.")
    parser.add_argument("--metadata-kind", action="append", help="Metadata kind to upload. Repeat or comma-separate values.")
    parser.add_argument("--mode", choices=["upsert", "replace"], default="upsert", help="Upload mode. Defaults to upsert.")
    parser.add_argument("--dry-run", action="store_true", help="Print target collections and document counts without writing.")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="MongoDB server selection timeout in milliseconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    try:
        input_path = Path(args.input) if args.input else latest_export_path()
        selected_kinds = parse_metadata_kinds(args.metadata_kind)
        summary = upload_bundle(input_path, resolve_config(args), selected_kinds, args.mode, args.dry_run)
    except Exception as exc:
        parser.exit(1, f"Upload failed: {exc}\n")

    print(json_util.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
