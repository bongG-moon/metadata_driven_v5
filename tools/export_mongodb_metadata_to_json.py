from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

from bson import json_util


EXPORT_FORMAT = "metadata_driven_v5.mongodb_metadata_bundle.v1"
DEFAULT_DATABASE = "datagov"
DEFAULT_OUTPUT_DIR = Path("metadata_exports")


class MetadataTarget:
    def __init__(self, key: str, display_name: str, env_name: str, default_collection: str):
        self.key = key
        self.display_name = display_name
        self.env_name = env_name
        self.default_collection = default_collection


METADATA_TARGETS: dict[str, MetadataTarget] = {
    "domain": MetadataTarget("domain", "domain metadata", "MONGODB_DOMAIN_COLLECTION", "agent_v4_domain_items"),
    "table-catalog": MetadataTarget("table-catalog", "table catalog metadata", "MONGODB_TABLE_CATALOG_COLLECTION", "agent_v4_table_catalog_items"),
    "main-flow-filter": MetadataTarget("main-flow-filter", "main variable/filter metadata", "MONGODB_MAIN_FLOW_FILTER_COLLECTION", "agent_v4_main_flow_filters"),
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


class MongoExportConfig:
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
        return list(METADATA_TARGETS)
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


def resolve_config(args: argparse.Namespace) -> MongoExportConfig:
    collections = {
        "domain": args.domain_collection or os.getenv("MONGODB_DOMAIN_COLLECTION", METADATA_TARGETS["domain"].default_collection),
        "table-catalog": args.table_catalog_collection or os.getenv("MONGODB_TABLE_CATALOG_COLLECTION", METADATA_TARGETS["table-catalog"].default_collection),
        "main-flow-filter": args.main_flow_filter_collection or os.getenv("MONGODB_MAIN_FLOW_FILTER_COLLECTION", METADATA_TARGETS["main-flow-filter"].default_collection),
    }
    return MongoExportConfig(
        mongo_uri=args.mongo_uri or os.getenv("MONGODB_URI", ""),
        database=args.database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collections=collections,
        timeout_ms=int(args.timeout_ms),
    )


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"mongodb_metadata_export_{stamp}.json"


def export_metadata_bundle(
    config: MongoExportConfig,
    selected_kinds: list[str],
    output_path: str | Path,
    status_filter: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    if not config.mongo_uri:
        raise RuntimeError("MONGODB_URI is required for MongoDB export.")

    pymongo = import_module("pymongo")
    client = pymongo.MongoClient(config.mongo_uri, serverSelectionTimeoutMS=config.timeout_ms)
    try:
        database = client[config.database]
        bundle: dict[str, Any] = {
            "_export_format": EXPORT_FORMAT,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source_database": config.database,
            "collections": {},
        }
        summary: dict[str, Any] = {
            "success": True,
            "output_path": str(Path(output_path)),
            "database": config.database,
            "collections": {},
        }
        query = {"status": status_filter} if status_filter else {}
        for kind in selected_kinds:
            target = METADATA_TARGETS[kind]
            collection_name = config.collections[kind]
            cursor = database[collection_name].find(query)
            try:
                cursor = cursor.sort("_id", 1)
            except AttributeError:
                pass
            if limit:
                cursor = cursor.limit(int(limit))
            docs = list(cursor)
            bundle["collections"][kind] = {
                "metadata_kind": kind,
                "display_name": target.display_name,
                "collection_name": collection_name,
                "document_count": len(docs),
                "documents": docs,
            }
            summary["collections"][kind] = {
                "collection_name": collection_name,
                "document_count": len(docs),
            }

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json_util.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export metadata-driven v5 MongoDB metadata collections to a portable JSON bundle.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file. Defaults to .env.")
    parser.add_argument("--mongo-uri", default="", help="MongoDB URI. Defaults to MONGODB_URI.")
    parser.add_argument("--database", default="", help="MongoDB database. Defaults to MONGODB_DATABASE or datagov.")
    parser.add_argument("--domain-collection", default="", help="Domain metadata collection name.")
    parser.add_argument("--table-catalog-collection", default="", help="Table catalog metadata collection name.")
    parser.add_argument("--main-flow-filter-collection", default="", help="Main variable/filter metadata collection name.")
    parser.add_argument("--metadata-kind", action="append", help="Metadata kind to export. Repeat or comma-separate values.")
    parser.add_argument("--status-filter", default="", help="Optional status value filter. Empty means export all documents.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max documents per collection. 0 means no limit.")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="MongoDB server selection timeout in milliseconds.")
    parser.add_argument("--output", default="", help="Output JSON path. Defaults to metadata_exports/mongodb_metadata_export_YYYYMMDD_HHMMSS.json.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    try:
        selected_kinds = parse_metadata_kinds(args.metadata_kind)
        output_path = Path(args.output) if args.output else default_output_path()
        summary = export_metadata_bundle(resolve_config(args), selected_kinds, output_path, args.status_filter, args.limit)
    except Exception as exc:
        parser.exit(1, f"Export failed: {exc}\n")

    print(json_util.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
