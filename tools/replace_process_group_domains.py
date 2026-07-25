from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_KNOWLEDGE_PATH = ROOT / "domain_knowledge.txt"
DOMAIN_FLOW_DIR = ROOT / "langflow_components" / "domain_saving_flow"
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_domain_items"


def load_dotenv(path: Path) -> None:
    """Load a local dotenv file without printing or returning secret values."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def parse_process_group_blocks(text: str) -> list[dict[str, Any]]:
    """Parse the explicit process-group authoring blocks in domain_knowledge.txt."""

    lines = text.splitlines()
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    section_pattern = re.compile(
        r"^section은 process_groups이고 key는 (.+?)이며 status는 (.+?)야\.$"
    )
    for index, raw_line in enumerate(lines):
        match = section_pattern.match(raw_line.strip())
        if not match:
            continue
        key = match.group(1).strip()
        status = match.group(2).strip()
        display_line = _next_nonempty_line(lines, index + 1)
        processes_line = _next_nonempty_line(lines, display_line[0] + 1)
        display_name, aliases = _parse_display_aliases(display_line[1])
        processes = _parse_processes(processes_line[1])
        if key in seen_keys:
            raise ValueError(f"process_groups key가 domain_knowledge.txt에 중복되었습니다: {key}")
        if not display_name or not aliases or not processes:
            raise ValueError(f"process_groups:{key} 블록의 display_name, aliases 또는 processes가 비어 있습니다.")
        seen_keys.add(key)
        raw_block = "\n".join(
            [
                _previous_nonempty_line(lines, index - 1),
                raw_line.strip(),
                display_line[1],
                processes_line[1],
                f"별칭마다 별도 item을 만들지 말고 process_groups:{key} 하나로 저장해.",
            ]
        )
        items.append(
            {
                "section": "process_groups",
                "key": key,
                "status": status,
                "payload": {
                    "display_name": display_name,
                    "aliases": aliases,
                    "processes": processes,
                },
                "_raw_text": raw_block,
            }
        )
    return items


def _next_nonempty_line(lines: list[str], start: int) -> tuple[int, str]:
    for index in range(start, len(lines)):
        value = lines[index].strip()
        if value:
            return index, value
    raise ValueError("process_groups 블록이 파일 끝에서 불완전합니다.")


def _previous_nonempty_line(lines: list[str], start: int) -> str:
    for index in range(start, -1, -1):
        value = lines[index].strip()
        if value:
            return value
    return "공정 그룹을 기존 canonical 항목에 재등록해서 보강해줘."


def _parse_display_aliases(line: str) -> tuple[str, list[str]]:
    prefix = "display_name은 "
    delimiter = "이고 aliases는 "
    if not line.startswith(prefix) or delimiter not in line:
        raise ValueError(f"display_name/aliases 형식을 해석할 수 없습니다: {line}")
    display_name, aliases_text = line[len(prefix) :].split(delimiter, 1)
    aliases_text = _strip_korean_sentence_ending(aliases_text)
    aliases = [value.strip() for value in aliases_text.split(",") if value.strip()]
    return display_name.strip(), list(dict.fromkeys(aliases))


def _parse_processes(line: str) -> list[str]:
    prefix = "processes는 OPER_NAME 값 "
    if not line.startswith(prefix):
        raise ValueError(f"processes 형식을 해석할 수 없습니다: {line}")
    values_text = _strip_korean_sentence_ending(line[len(prefix) :])
    if values_text.endswith(" 하나"):
        values_text = values_text[: -len(" 하나")].rstrip()
    return [value.strip() for value in values_text.split(",") if value.strip()]


def _strip_korean_sentence_ending(value: str) -> str:
    text = value.strip()
    for suffix in ("이야.", "야."):
        if text.endswith(suffix):
            return text[: -len(suffix)].rstrip()
    raise ValueError(f"문장 종결 형식을 해석할 수 없습니다: {value}")


def run_replacement(
    items: list[dict[str, Any]],
    *,
    apply: bool,
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> dict[str, Any]:
    """Run the actual Domain Saving Flow normalization, similarity, and writer functions."""

    if not mongo_uri:
        raise RuntimeError("MONGODB_URI가 없어 Domain Saving Flow를 실행할 수 없습니다.")

    sys.path.insert(0, str(ROOT))
    from tools import validate_representative_questions as harness

    harness.install_lfx_stubs()
    request_loader = harness.load_module(
        DOMAIN_FLOW_DIR / "00_domain_saving_request_loader.py"
    )
    normalizer = harness.load_module(
        DOMAIN_FLOW_DIR / "04_domain_saving_result_normalizer.py"
    )
    similarity = harness.load_module(
        DOMAIN_FLOW_DIR / "05_domain_similarity_checker.py"
    )
    writer = harness.load_module(
        DOMAIN_FLOW_DIR / "07_domain_review_writer.py"
    )

    results: list[dict[str, Any]] = []
    for source_item in items:
        item = {
            key: value
            for key, value in source_item.items()
            if not str(key).startswith("_")
        }
        raw_text = str(source_item.get("_raw_text") or "")
        payload = request_loader.build_request(
            raw_text,
            duplicate_action="replace",
            dry_run=not apply,
        )
        payload = normalizer.normalize_authoring(
            payload,
            {
                "items": [item],
                "needs_more_input": False,
                "missing_information": [],
                "assumptions": [],
            },
        )
        payload = similarity.check_similarity(
            payload,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_name=collection_name,
        )
        payload = writer.review_and_write(
            payload,
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_name=collection_name,
        )
        write_result = payload.get("write_result")
        write_result = write_result if isinstance(write_result, dict) else {}
        results.append(
            {
                "key": item["key"],
                "status": write_result.get("status"),
                "success": bool(write_result.get("success")),
                "saved_count": int(write_result.get("saved_count") or 0),
                "operations": write_result.get("operation_by_key") or [],
                "errors": write_result.get("errors") or [],
                "identity_resolution": (
                    payload.get("trace", {}).get("identity_resolution", {})
                    if isinstance(payload.get("trace"), dict)
                    else {}
                ),
            }
        )
    failed = [item for item in results if not item["success"]]
    return {
        "status": "ok" if not failed else ("partial" if len(failed) < len(results) else "error"),
        "mode": "apply" if apply else "dry_run",
        "database": mongo_database,
        "collection_name": collection_name,
        "requested_count": len(results),
        "saved_count": sum(int(item["saved_count"]) for item in results),
        "success_count": len(results) - len(failed),
        "failure_count": len(failed),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="domain_knowledge.txt의 process_groups 블록을 실제 Domain Saving Flow의 replace 경로로 검증·저장합니다."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="지정하면 MongoDB에 실제 저장합니다. 생략하면 Dry Run입니다.",
    )
    parser.add_argument(
        "--keys",
        nargs="*",
        default=[],
        help="일부 canonical key만 실행할 때 사용합니다.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="비밀값을 제외한 실행 결과 JSON 저장 경로입니다.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    items = parse_process_group_blocks(
        DOMAIN_KNOWLEDGE_PATH.read_text(encoding="utf-8")
    )
    requested_keys = {str(key).strip().upper() for key in args.keys if str(key).strip()}
    if requested_keys:
        items = [
            item
            for item in items
            if str(item.get("key") or "").upper() in requested_keys
        ]
        missing_keys = sorted(
            requested_keys
            - {str(item.get("key") or "").upper() for item in items}
        )
        if missing_keys:
            raise ValueError(
                f"domain_knowledge.txt에서 key를 찾지 못했습니다: {', '.join(missing_keys)}"
            )

    report = run_replacement(
        items,
        apply=bool(args.apply),
        mongo_uri=os.getenv("MONGODB_URI", ""),
        mongo_database=os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name=os.getenv("MONGODB_DOMAIN_COLLECTION", DEFAULT_COLLECTION),
    )
    output_text = json.dumps(report, ensure_ascii=False, indent=2)
    print(output_text)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
