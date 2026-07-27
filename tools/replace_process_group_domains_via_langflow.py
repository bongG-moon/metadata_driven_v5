from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bson import json_util
from pymongo import MongoClient

try:
    from .replace_process_group_domains import (
        DEFAULT_COLLECTION,
        DEFAULT_DATABASE,
        DOMAIN_KNOWLEDGE_PATH,
        ROOT,
        load_dotenv,
        parse_process_group_blocks,
    )
except ImportError:
    from replace_process_group_domains import (
        DEFAULT_COLLECTION,
        DEFAULT_DATABASE,
        DOMAIN_KNOWLEDGE_PATH,
        ROOT,
        load_dotenv,
        parse_process_group_blocks,
    )


DEFAULT_FLOW_ID = "ea16279a-25ef-596e-a07a-4c1f46323662"
DEFAULT_BASE_URL = "http://127.0.0.1:17860"


def _configured_api_key() -> str:
    return (
        os.getenv("GOOGLE_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )


def _langflow_headers(session: requests.Session, base_url: str) -> dict[str, str]:
    langflow_api_key = os.getenv("LANGFLOW_API_KEY", "").strip()
    if langflow_api_key:
        return {"x-api-key": langflow_api_key}

    response = session.get(f"{base_url.rstrip('/')}/api/v1/auto_login", timeout=15)
    response.raise_for_status()
    access_token = str(response.json().get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Langflow auto-login response에 access_token이 없습니다.")
    return {"Authorization": f"Bearer {access_token}"}


def _message_from_run_response(payload: dict[str, Any]) -> str:
    messages: list[str] = []
    for output_group in payload.get("outputs") or []:
        if not isinstance(output_group, dict):
            continue
        for output in output_group.get("outputs") or []:
            if not isinstance(output, dict):
                continue
            results = output.get("results")
            results = results if isinstance(results, dict) else {}
            message = results.get("message")
            if isinstance(message, dict):
                text = str(message.get("text") or message.get("message") or "").strip()
                if text:
                    messages.append(text)
            output_values = output.get("outputs")
            if isinstance(output_values, dict):
                message_value = output_values.get("message")
                if isinstance(message_value, dict):
                    text = str(message_value.get("message") or message_value.get("text") or "").strip()
                    if text:
                        messages.append(text)
    return next((value for value in messages if value), "")


def _run_group(
    *,
    session: requests.Session,
    headers: dict[str, str],
    base_url: str,
    flow_id: str,
    item: dict[str, Any],
    dry_run: bool,
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
    model_api_key: str,
    timeout: int,
) -> dict[str, Any]:
    mode = "dryrun" if dry_run else "live"
    request_payload = {
        "input_value": item["_raw_text"],
        "input_type": "chat",
        "output_type": "chat",
        "session_id": f"process-group-{mode}-{item['key']}-{uuid.uuid4().hex}",
        "tweaks": {
            "Request-domain": {
                "duplicate_action": "replace",
                "dry_run": dry_run,
            },
            "LanguageModelExtract-domain": {
                "api_key": model_api_key,
            },
            "Matcher-domain": {
                "mongo_uri": mongo_uri,
                "mongo_database": mongo_database,
                "collection_name": collection_name,
            },
            "Writer-domain": {
                "mongo_uri": mongo_uri,
                "mongo_database": mongo_database,
                "collection_name": collection_name,
            },
            "ChatInput-domain": {
                "should_store_message": False,
            },
            "ChatOutput-domain": {
                "should_store_message": False,
            },
        },
    }
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/run/{flow_id}",
        headers=headers,
        json=request_payload,
        timeout=timeout,
    )
    response.raise_for_status()
    response_payload = response.json()
    message = _message_from_run_response(response_payload)
    expected_key = f"process_groups:{item['key']}"
    if not message:
        raise RuntimeError(f"{item['key']} Flow 응답에서 최종 Message를 찾지 못했습니다.")
    if expected_key not in message:
        raise RuntimeError(
            f"{item['key']} Flow 응답이 예상 key를 포함하지 않습니다: {expected_key}"
        )
    if "오류" in message or "실패" in message:
        raise RuntimeError(f"{item['key']} Flow가 오류 메시지를 반환했습니다: {message[:500]}")
    if dry_run and "Dry Run" not in message:
        raise RuntimeError(f"{item['key']} Flow가 Dry Run 결과를 반환하지 않았습니다.")
    if not dry_run and "저장" not in message:
        raise RuntimeError(f"{item['key']} Flow가 실제 저장 결과를 반환하지 않았습니다.")
    return {
        "key": item["key"],
        "mode": mode,
        "http_status": response.status_code,
        "expected_key": expected_key,
        "message": message,
    }


def _payload_projection(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return {
        "display_name": payload.get("display_name"),
        "aliases": payload.get("aliases"),
        "field": payload.get("field"),
        "processes": payload.get("processes"),
    }


def _verify_document(
    collection: Any,
    item: dict[str, Any],
) -> dict[str, Any]:
    document_id = f"domain:process_groups:{item['key']}"
    document = collection.find_one({"_id": document_id})
    if not isinstance(document, dict):
        raise RuntimeError(f"MongoDB에 저장 문서가 없습니다: {document_id}")
    expected_payload = deepcopy(item["payload"])
    actual_payload = _payload_projection(document)
    if document.get("section") != "process_groups":
        raise RuntimeError(f"{document_id} section이 process_groups가 아닙니다.")
    if document.get("key") != item["key"]:
        raise RuntimeError(f"{document_id} key가 원문과 다릅니다.")
    if document.get("status") != "active":
        raise RuntimeError(f"{document_id} status가 active가 아닙니다.")
    if actual_payload != expected_payload:
        raise RuntimeError(
            f"{document_id} payload가 원문과 다릅니다: "
            f"expected={expected_payload!r}, actual={actual_payload!r}"
        )
    return {
        "_id": document_id,
        "section": document.get("section"),
        "key": document.get("key"),
        "status": document.get("status"),
        "payload": actual_payload,
    }


def _backup_documents(path: Path, documents: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json_util.dumps(
            {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "query": {"section": "process_groups"},
                "count": len(documents),
                "documents": documents,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def execute(
    *,
    items: list[dict[str, Any]],
    base_url: str,
    flow_id: str,
    apply: bool,
    backup_path: Path,
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
    model_api_key: str,
    timeout: int,
) -> dict[str, Any]:
    if not items:
        raise RuntimeError("실행할 process_groups 항목이 없습니다.")
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI가 필요합니다.")
    if not model_api_key:
        raise RuntimeError("GOOGLE_API_KEY, GEMINI_API_KEY 또는 LLM_API_KEY가 필요합니다.")

    session = requests.Session()
    headers = _langflow_headers(session, base_url)
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    collection = client[mongo_database][collection_name]
    before_documents = list(collection.find({"section": "process_groups"}))
    dry_runs: list[dict[str, Any]] = []
    live_runs: list[dict[str, Any]] = []
    verified_documents: list[dict[str, Any]] = []
    deletion_count = 0

    try:
        for index, item in enumerate(items, start=1):
            print(f"[dry-run {index}/{len(items)}] {item['key']}", flush=True)
            dry_runs.append(
                _run_group(
                    session=session,
                    headers=headers,
                    base_url=base_url,
                    flow_id=flow_id,
                    item=item,
                    dry_run=True,
                    mongo_uri=mongo_uri,
                    mongo_database=mongo_database,
                    collection_name=collection_name,
                    model_api_key=model_api_key,
                    timeout=timeout,
                )
            )

        if not apply:
            return {
                "status": "ok",
                "mode": "dry_run",
                "requested_count": len(items),
                "existing_process_group_count": len(before_documents),
                "dry_run_success_count": len(dry_runs),
                "dry_runs": dry_runs,
            }

        _backup_documents(backup_path, before_documents)
        deleted = collection.delete_many({"section": "process_groups"})
        deletion_count = int(deleted.deleted_count)
        remaining = collection.count_documents({"section": "process_groups"})
        if remaining:
            raise RuntimeError(f"기존 process_groups 삭제 후 {remaining}건이 남았습니다.")
        print(
            f"[delete] process_groups {deletion_count}건 삭제, backup={backup_path}",
            flush=True,
        )

        for index, item in enumerate(items, start=1):
            print(f"[live {index}/{len(items)}] {item['key']}", flush=True)
            live_runs.append(
                _run_group(
                    session=session,
                    headers=headers,
                    base_url=base_url,
                    flow_id=flow_id,
                    item=item,
                    dry_run=False,
                    mongo_uri=mongo_uri,
                    mongo_database=mongo_database,
                    collection_name=collection_name,
                    model_api_key=model_api_key,
                    timeout=timeout,
                )
            )
            verified_documents.append(_verify_document(collection, item))

        expected_ids = {f"domain:process_groups:{item['key']}" for item in items}
        final_documents = list(collection.find({"section": "process_groups"}, {"_id": 1}))
        actual_ids = {str(document.get("_id")) for document in final_documents}
        missing_ids = sorted(expected_ids - actual_ids)
        extra_ids = sorted(actual_ids - expected_ids)
        if missing_ids or extra_ids or len(final_documents) != len(items):
            raise RuntimeError(
                "최종 process_groups inventory가 원문과 다릅니다: "
                f"missing={missing_ids}, extra={extra_ids}, count={len(final_documents)}"
            )

        return {
            "status": "ok",
            "mode": "apply",
            "database": mongo_database,
            "collection_name": collection_name,
            "requested_count": len(items),
            "before_process_group_count": len(before_documents),
            "deleted_count": deletion_count,
            "dry_run_success_count": len(dry_runs),
            "live_success_count": len(live_runs),
            "final_process_group_count": len(final_documents),
            "backup_path": str(backup_path),
            "dry_runs": dry_runs,
            "live_runs": live_runs,
            "verified_documents": verified_documents,
            "rollback_performed": False,
        }
    except Exception:
        if apply:
            collection.delete_many({"section": "process_groups"})
            if before_documents:
                collection.insert_many(before_documents)
            print(
                f"[rollback] 기존 process_groups {len(before_documents)}건을 복원했습니다.",
                flush=True,
            )
        raise
    finally:
        client.close()
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "domain_knowledge.txt의 공정그룹을 실제 Langflow Domain Saving Flow의 "
            "replace 경로로 dry-run한 뒤 선택적으로 MongoDB에 다시 저장합니다."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--flow-id", default=DEFAULT_FLOW_ID)
    parser.add_argument(
        "--backup",
        default="",
        help="기존 process_groups 백업 경로입니다.",
    )
    parser.add_argument(
        "--output",
        default="validation_outputs/process_group_langflow_replace_20260727.json",
    )
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = (
        Path(args.backup)
        if args.backup
        else ROOT / "metadata_exports" / f"process_groups_before_replace_{timestamp}.json"
    )
    if not backup_path.is_absolute():
        backup_path = ROOT / backup_path

    items = parse_process_group_blocks(DOMAIN_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    report = execute(
        items=items,
        base_url=str(args.base_url),
        flow_id=str(args.flow_id),
        apply=bool(args.apply),
        backup_path=backup_path,
        mongo_uri=os.getenv("MONGODB_URI", ""),
        mongo_database=os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name=os.getenv("MONGODB_DOMAIN_COLLECTION", DEFAULT_COLLECTION),
        model_api_key=_configured_api_key(),
        timeout=max(30, int(args.timeout)),
    )
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key not in {"dry_runs", "live_runs", "verified_documents"}}, ensure_ascii=False, indent=2))
    print(f"report={output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
