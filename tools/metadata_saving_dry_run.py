"""Generate dry-run metadata saving reports from raw text files.

This tool intentionally does not write MongoDB metadata. Without real LLM
responses it emits Korean prompts and block-level reports so an operator can
review what would be sent to the saving flows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reference_runtime.metadata_saving import run_authoring_dry_run, split_raw_text_blocks, timestamp_utc, write_json_report


DEFAULT_INPUTS = {
    "domain": "domain_knowledge.txt",
    "table_catalog": "data_catalog.txt",
    "main_flow_filter": "main_variable.txt",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run metadata saving flow inputs without MongoDB writes.")
    parser.add_argument("--workspace", default=".", help="Workspace root containing txt metadata inputs.")
    parser.add_argument("--output-dir", default="", help="Report directory. Defaults to validation_runs/metadata_saving_dry_run/<timestamp>.")
    parser.add_argument("--metadata-type", choices=sorted(DEFAULT_INPUTS), action="append", help="Limit to one or more metadata types.")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else workspace / "validation_runs" / "metadata_saving_dry_run" / timestamp_utc()
    metadata_types = args.metadata_type or list(DEFAULT_INPUTS)

    summary = {
        "workspace": str(workspace),
        "output_dir": str(output_dir),
        "metadata_types": metadata_types,
        "written_to_mongodb": False,
        "results": [],
    }

    for metadata_type in metadata_types:
        input_path = workspace / DEFAULT_INPUTS[metadata_type]
        raw_text = input_path.read_text(encoding="utf-8")
        blocks = split_raw_text_blocks(raw_text, metadata_type=metadata_type)
        type_report = {
            "metadata_type": metadata_type,
            "input_path": str(input_path),
            "block_count": len(blocks),
            "blocks": [],
        }
        for index, block in enumerate(blocks, start=1):
            result = run_authoring_dry_run(metadata_type, block)
            block_report = {
                "index": index,
                "raw_text_preview": result["trace"]["raw_text_preview"],
                "status": "needs_llm",
                "message": result["write_result"]["message"],
                "warnings": result.get("warnings", []),
                "prompt_keys": sorted(result.get("prompts", {}).keys()),
            }
            type_report["blocks"].append(block_report)

            prompt_path = output_dir / metadata_type / f"block_{index:03d}_prompts.json"
            write_json_report(prompt_path, result.get("prompts", {}))

        report_path = output_dir / f"{metadata_type}_dry_run_report.json"
        write_json_report(report_path, type_report)
        summary["results"].append(
            {
                "metadata_type": metadata_type,
                "block_count": len(blocks),
                "report_path": str(report_path),
            }
        )

    summary_path = output_dir / "SUMMARY.json"
    write_json_report(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
