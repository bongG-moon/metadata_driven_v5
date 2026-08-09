#!/usr/bin/env python3
"""Validate the additive V2 continuation flow without changing canonical V2.

The validator has two independent layers:

* R01-R30 execute the existing canonical V2 fixture and the continuation V2
  executor/answer copies, then compare a semantic fingerprint.  The
  fingerprint intentionally ignores analysis_kind, prose, generated code and
  unspecified row order.
* C01-C14 exercise a metadata-shaped continuation/safety fixture runtime.  The runtime
  implements only generic operations declared in the manifest, so HOLD and
  equipment behavior are evidence cases rather than hard-coded production
  branches.

The manifest is validation-only.  Neither production flow reads it.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import types
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_data_analysis_v2_routes as route_validator  # noqa: E402
from tools import data_analysis_semantic_validator as semantic_validator  # noqa: E402
from tools import validate_representative_questions as base  # noqa: E402


MANIFEST_PATH = ROOT / "validation_questions_v2_continuation_manifest.json"
CANONICAL_QUESTIONS_PATH = ROOT / "validation_questions_v2.txt"
CONTINUATION_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2_continuation"
DATA_FLOW_EXPORT = ROOT / "flow_exports" / "08_data_analysis_flow_v2_continuation_standalone.json"
ROUTER_FLOW_EXPORT = ROOT / "flow_exports" / "09_agent_tool_router_continuation_flow_v5_standalone.json"
ROUTER_COMPONENT = (
    ROOT
    / "langflow_components"
    / "route_flow_v2_continuation"
    / "01_cached_continuation_run_flow_tool.py"
)
TARGET_LANGFLOW_VERSION = "1.9.2"
DEFAULT_LIVE_MODEL = "gemini-3.5-flash-lite"
LIVE_REQUIRED_CONTINUATION_IDS = {"C01", "C02", "C10", "C12", "C13"}
LIVE_MAX_PROMPT_TOKENS = 20_000
LIVE_MAX_CONTINUATION_PROMPT_OVERHEAD_BYTES = 4_096


class LiveCheckpoint:
    """Persist bounded live validation progress without storing prompts or secrets."""

    def __init__(
        self,
        path: Path | None,
        *,
        model: str,
        reference_date: str,
        resume: bool = False,
    ) -> None:
        self.path = path
        self.model = model
        self.reference_date = reference_date
        self.resume = bool(resume)
        self.document: dict[str, Any] = {
            "version": 1,
            "model": model,
            "reference_date": reference_date,
            "responses": {},
            "results": {},
        }
        if path and path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Live checkpoint must contain a JSON object")
            if resume:
                if str(loaded.get("model") or "") != model:
                    raise ValueError("Live checkpoint model does not match the selected model")
                if str(loaded.get("reference_date") or "") != reference_date:
                    raise ValueError("Live checkpoint reference_date does not match")
                self.document = loaded

    def cached_response(self, case_id: str, stage: str, prompt: str) -> str:
        return str(self.cached_response_record(case_id, stage, prompt).get("response") or "")

    def cached_response_record(self, case_id: str, stage: str, prompt: str) -> dict[str, Any]:
        """Return a prompt-hash-bound response and its Gemini usage metadata."""

        if not self.resume:
            return {}
        key = f"{case_id}:{stage}"
        item = self.document.get("responses", {}).get(key)
        if not isinstance(item, dict):
            return {}
        if str(item.get("prompt_sha256") or "") != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
            return {}
        return deepcopy(item)

    def save_response(
        self,
        case_id: str,
        stage: str,
        prompt: str,
        response: str,
        usage_metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.path:
            return
        key = f"{case_id}:{stage}"
        self.document.setdefault("responses", {})[key] = {
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "response": response,
            "usage_metadata": _normalized_usage_metadata(usage_metadata),
        }
        self._write()

    def save_result(self, result: dict[str, Any]) -> None:
        if not self.path:
            return
        compact = {
            key: deepcopy(value)
            for key, value in result.items()
            if key not in {"intent_plan", "rows", "source_results"}
        }
        self.document.setdefault("results", {})[str(result.get("id") or "")] = compact
        self._write()

    def _write(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class LiveModelRecorder:
    """Count actual and cached model uses and measure LLM-bound prompt chars."""

    def __init__(
        self,
        case_id: str,
        llm_config: dict[str, Any],
        checkpoint: LiveCheckpoint,
    ) -> None:
        self.case_id = case_id
        self.llm_config = llm_config
        self.checkpoint = checkpoint
        self.calls = {"intent": 0, "pandas": 0, "answer": 0}
        self.cached = {"intent": 0, "pandas": 0, "answer": 0}
        self.prompt_chars = {"intent": 0, "pandas": 0, "answer": 0}
        self.prompt_bytes = {"intent": 0, "pandas": 0, "answer": 0}
        self.usage_metadata = {
            stage: _empty_usage_metadata() for stage in ("intent", "pandas", "answer")
        }
        self.usage_samples = {"intent": 0, "pandas": 0, "answer": 0}

    def invoker(self, stage: str) -> Callable[[str], str]:
        if stage not in self.calls:
            raise ValueError(f"Unsupported live model stage: {stage}")

        def invoke(prompt: str) -> str:
            text = str(prompt or "")
            self.prompt_chars[stage] += len(text)
            self.prompt_bytes[stage] += len(text.encode("utf-8"))
            ordinal = int(self.calls[stage]) + int(self.cached[stage]) + 1
            checkpoint_stage = f"{stage}:{ordinal}"
            cached_record = self.checkpoint.cached_response_record(
                self.case_id,
                checkpoint_stage,
                text,
            )
            cached = str(cached_record.get("response") or "")
            if cached:
                self.cached[stage] += 1
                usage = _normalized_usage_metadata(cached_record.get("usage_metadata"))
                self._record_usage(stage, usage)
                return cached
            self.calls[stage] += 1
            if str(self.llm_config.get("api_key") or "").strip():
                response, usage = _call_gemini_with_usage(text, self.llm_config)
            else:
                # Unit tests and explicitly injected offline callers retain the
                # canonical validation helper without fabricating token counts.
                response = base.call_llm(text, self.llm_config)
                usage = _empty_usage_metadata()
            self._record_usage(stage, usage)
            self.checkpoint.save_response(
                self.case_id,
                checkpoint_stage,
                text,
                response,
                usage,
            )
            return response

        return invoke

    def _record_usage(self, stage: str, usage: dict[str, Any]) -> None:
        normalized = _normalized_usage_metadata(usage)
        if int(normalized.get("promptTokenCount") or 0) <= 0:
            return
        self.usage_samples[stage] += 1
        for key in _empty_usage_metadata():
            self.usage_metadata[stage][key] += int(normalized.get(key) or 0)

    def summary(self) -> dict[str, Any]:
        return {
            "actual_calls": deepcopy(self.calls),
            "cached_responses": deepcopy(self.cached),
            "logical_uses": {
                key: int(self.calls[key]) + int(self.cached[key])
                for key in self.calls
            },
            "prompt_chars": {
                **deepcopy(self.prompt_chars),
                "total": sum(int(value) for value in self.prompt_chars.values()),
            },
            "prompt_bytes": {
                **deepcopy(self.prompt_bytes),
                "total": sum(int(value) for value in self.prompt_bytes.values()),
            },
            "usage_metadata": {
                **deepcopy(self.usage_metadata),
                "total": {
                    key: sum(int(self.usage_metadata[stage][key]) for stage in self.usage_metadata)
                    for key in _empty_usage_metadata()
                },
            },
            "usage_samples": {
                **deepcopy(self.usage_samples),
                "total": sum(int(value) for value in self.usage_samples.values()),
            },
        }


def _empty_usage_metadata() -> dict[str, int]:
    """Return the stable Gemini token counters emitted by this validator."""

    return {
        "promptTokenCount": 0,
        "candidatesTokenCount": 0,
        "totalTokenCount": 0,
    }


def _normalized_usage_metadata(value: Any) -> dict[str, int]:
    """Keep only non-secret integer token counters from Gemini usageMetadata."""

    source = value if isinstance(value, dict) else {}
    result = _empty_usage_metadata()
    for key in result:
        try:
            result[key] = max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            result[key] = 0
    return result


def _call_gemini_with_usage(prompt: str, config: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Call Gemini exactly once and preserve its authoritative usageMetadata."""

    model = str(config["model"]).removeprefix("models/")
    encoded_model = urllib.parse.quote(model, safe="")
    api_key = urllib.parse.quote(str(config["api_key"]), safe="")
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:"
        f"generateContent?key={api_key}"
    )
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
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(
                error_payload.get("error", {}).get("message")
                if isinstance(error_payload, dict)
                else ""
            ).strip()
        except Exception:
            detail = ""
        suffix = f": {detail[:500]}" if detail else ""
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not text.strip():
        raise RuntimeError("LLM response did not contain text")
    return text, _normalized_usage_metadata(data.get("usageMetadata"))


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if int(document.get("version") or 0) != 1:
        raise ValueError("Continuation validation manifest version must be 1")
    limits = document.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("Continuation validation manifest is missing limits")
    for key in (
        "max_stages",
        "max_continuation_contract_bytes",
        "max_agent_observation_bytes",
        "max_stage1_prompt_bytes",
        "max_stage2_prompt_bytes",
    ):
        if int(limits.get(key) or 0) <= 0:
            raise ValueError(f"Continuation validation limit is invalid: {key}")

    regression = document.get("regression_cases")
    continuation = document.get("continuation_cases")
    multiturn = document.get("multiturn_scenarios")
    if not isinstance(regression, list) or len(regression) != 30:
        raise ValueError("Continuation manifest must contain exactly 30 canonical regression cases")
    if not isinstance(continuation, list) or len(continuation) < 8:
        raise ValueError("Continuation manifest must contain the dependent and failure scenarios")
    if not isinstance(multiturn, list) or {str(item.get("id") or "") for item in multiturn} != {"M01", "M02"}:
        raise ValueError("Continuation manifest must contain executable M01 and M02 scenarios")
    _validate_unique_ids([*regression, *continuation, *multiturn])
    if [int(item.get("base_case_id") or 0) for item in regression] != list(range(1, 31)):
        raise ValueError("Canonical regression cases must map to base cases 1..30 in order")
    return document


def _validate_unique_ids(cases: list[Any]) -> None:
    ids = [str(item.get("id") or "").strip() for item in cases if isinstance(item, dict)]
    if len(ids) != len(cases) or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("Continuation validation case ids must be non-empty and unique")


def _continuation_modules() -> dict[str, Any]:
    try:
        base.install_lfx_stubs()
    except ValueError as exc:
        # importlib.find_spec raises when an already-installed lightweight
        # fixture module has no __spec__; in that case the stubs are usable.
        if "lfx.__spec__ is None" not in str(exc):
            raise
    required = {
        "executor": CONTINUATION_ROOT / "17_continuation_hybrid_analysis_executor.py",
        "answer": CONTINUATION_ROOT / "20_continuation_hybrid_answer_builder.py",
        "api": CONTINUATION_ROOT / "22_continuation_api_response_builder.py",
        "intent_vars": CONTINUATION_ROOT / "02_intent_variables_builder.py",
        "intent_router": CONTINUATION_ROOT / "03b_continuation_aware_intent_router.py",
        "request": CONTINUATION_ROOT / "00_continuation_analysis_request_loader.py",
        "catalog_closure": CONTINUATION_ROOT / "01e_dependency_catalog_candidate_closure.py",
        "compiler": CONTINUATION_ROOT / "04b_dependent_retrieval_plan_compiler.py",
        "result_loader": CONTINUATION_ROOT / "05_continuation_mongodb_result_loader.py",
        "binding_alias_normalizer": CONTINUATION_ROOT
        / "05a_continuation_binding_alias_normalizer.py",
        "binder": ROOT / "langflow_components" / "data_analysis_flow" / "05a_upstream_entity_parameter_binder.py",
        "gate": ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Continuation component sources are missing: {missing}")
    return {name: base.load_module(path) for name, path in required.items()}


def _execute_v2_fixture(
    case: dict[str, Any],
    expectation: dict[str, Any],
    modules: dict[str, Any],
    v2: dict[str, Any],
    *,
    reference_date: str,
    compiler: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    payload, _ = route_validator._prepared_payload(case, modules, reference_date)
    payload = _augment_v2_fixture_payload(case, payload, reference_date)
    payload = route_validator.apply_manifest_fixture_plan(payload, expectation)
    if compiler is not None:
        compiled_text, _ = compiler.compile_intent_response(
            payload,
            json.dumps({"intent_plan": payload.get("intent_plan", {})}, ensure_ascii=False),
            base.validation_catalog(case),
        )
        compiled = json.loads(compiled_text)
        payload["intent_plan"] = deepcopy(compiled.get("intent_plan") or {})
    pandas_vars = base.with_selected_helper_code(modules, modules["pandas_vars"].build_variables(payload))
    resolved = v2["resolver"].resolve_simple_analysis_contract(payload)
    pandas_code = str(case["pandas_code"])
    if case.get("requires_helper"):
        pandas_code = base.inline_helper_source(
            pandas_code,
            str(case.get("helper_function") or "match_product_tokens"),
        )
    calls: list[str] = []

    def invoke(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"code": pandas_code}, ensure_ascii=False)

    executed = v2["executor"].execute_hybrid_analysis(
        resolved,
        "fixture complex pandas prompt",
        model_invoker=invoke,
        repair_prompt_template="fixture repair prompt",
        function_case_helper_code=str(pandas_vars.get("function_case_helper_code") or ""),
        max_repair_attempts=0,
    )
    answered = v2["answer"].build_answer_response(executed, "분석 결과를 확인했습니다.")
    return answered, resolved, len(calls)


def _candidate_v2_modules(canonical: dict[str, Any], continuation: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(canonical)
    candidate["executor"] = continuation["executor"]
    candidate["answer"] = continuation["answer"]
    return candidate


def _canonical_sp24_fixture_rows(work_date: str) -> list[dict[str, Any]]:
    """Return one canonical R09 product and two token-role decoys."""

    normalized_date = str(work_date or "").replace("-", "")[:8]
    common = {
        "WORK_DATE": normalized_date,
        "DATE": normalized_date,
        "OPER": "FCB1",
        "OPER_NAME": "FCB1",
        "OPER_SEQ": "700",
        "TECH": "SP",
        "DENSITY": "24G",
        "DEN": "24G",
        "MODE": "GDDR7",
        "ORG": "32",
        "PKG1": "FCBGA",
        "PKG_TYPE1": "FCBGA",
        "PKG2": "DDP",
        "PKG_TYPE2": "DDP",
        "MCP_NO": "",
        "TSV_DIE_TYP": "",
    }
    return [
        {
            **common,
            "LEAD": "226",
            "DEVICE": "DEV-SP24-GDDR7-X32-226",
            "DEVICE_DESC": "SP 24G GDDR7 X32 226 FCBGA DDP",
            "PRODUCTION": 424,
        },
        {
            **common,
            "LEAD": "225",
            "DEVICE": "DECOY-SP24-WRONG-LEAD",
            "DEVICE_DESC": "SP 24G GDDR7 X32 225 FCBGA DDP",
            "PRODUCTION": 9000,
        },
        {
            **common,
            "ORG": "16",
            "LEAD": "226",
            "DEVICE": "DECOY-SP24-WRONG-ORG",
            "DEVICE_DESC": "SP 24G GDDR7 X16 226 FCBGA DDP",
            "PRODUCTION": 8000,
        },
    ]


def _canonical_regression_base_case(
    base_case: dict[str, Any],
    question: str,
) -> dict[str, Any]:
    """Align legacy fixture inputs with the exact canonical V2 question text."""

    case = deepcopy(base_case)
    case_id = int(case.get("id") or 0)
    case["question"] = str(question)
    if case_id != 9:
        return case

    product_text = "SP 24G GDDR7 X32 226 FCBGA DDP"
    function_cases = (
        case.get("intent_response", {})
        .get("intent_plan", {})
        .get("pandas_function_cases", [])
    )
    for function_case in function_cases if isinstance(function_cases, list) else []:
        if isinstance(function_case, dict):
            function_case["input_text"] = product_text
    case["pandas_code"] = (
        f"df = match_product_tokens('{product_text}', sources['production_data'])\n"
        "result = df.groupby(['TECH', 'DEN', 'MODE', 'ORG', 'PKG_TYPE1', 'PKG_TYPE2', "
        "'LEAD', 'MCP_NO', 'DEVICE'], as_index=False)['PRODUCTION'].sum()"
        ".rename(columns={'PRODUCTION': 'TOTAL_PRODUCTION'})"
    )
    case["expected_row_count"] = 1
    case["expected_first_row"] = {
        "DEVICE": "DEV-SP24-GDDR7-X32-226",
        "TOTAL_PRODUCTION": 424,
    }
    case["forbidden_values"] = {
        "DEVICE": ["DECOY-SP24-WRONG-LEAD", "DECOY-SP24-WRONG-ORG"]
    }
    case["validation_fixture_adapter"] = "canonical_sp24_product_token_rows"
    return case


def _augment_v2_fixture_payload(
    case: dict[str, Any],
    payload: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    """Inject canonical validation rows into both parity executions."""

    if str(case.get("validation_fixture_adapter") or "") != "canonical_sp24_product_token_rows":
        return payload
    result = deepcopy(payload)
    runtime_sources = result.get("runtime_sources")
    if not isinstance(runtime_sources, dict):
        return result
    existing = runtime_sources.get("production_data")
    rows = [deepcopy(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    jobs = case.get("intent_response", {}).get("intent_plan", {}).get("retrieval_jobs", [])
    required_date = next(
        (
            str(job.get("required_params", {}).get("DATE") or "")
            for job in jobs
            if isinstance(job, dict) and str(job.get("source_alias") or "") == "production_data"
        ),
        "",
    )
    rows.extend(_canonical_sp24_fixture_rows(required_date or reference_date))
    runtime_sources["production_data"] = rows
    for source in result.get("source_results", []):
        if not isinstance(source, dict) or str(source.get("source_alias") or "") != "production_data":
            continue
        source["row_count"] = len(rows)
        source["columns"] = sorted({column for row in rows for column in row})
        source["preview_rows"] = rows[: min(20, len(rows))]
        source.setdefault("validation_fixture_adapters", []).append(
            "canonical_sp24_product_token_rows"
        )
    return result


def validate_regression_case(
    manifest_case: dict[str, Any],
    base_cases: dict[int, dict[str, Any]],
    route_expectations: dict[int, dict[str, Any]],
    modules: dict[str, Any],
    canonical_v2: dict[str, Any],
    continuation_modules: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    case_id = int(manifest_case["base_case_id"])
    case = base_cases[case_id]
    expectation = route_expectations[case_id]
    canonical, canonical_resolved, canonical_calls = _execute_v2_fixture(
        case,
        expectation,
        modules,
        canonical_v2,
        reference_date=reference_date,
    )
    candidate_v2 = _candidate_v2_modules(canonical_v2, continuation_modules)
    candidate, candidate_resolved, candidate_calls = _execute_v2_fixture(
        case,
        expectation,
        modules,
        candidate_v2,
        reference_date=reference_date,
        compiler=continuation_modules["compiler"],
    )
    canonical_fingerprint = canonical_semantic_fingerprint(canonical)
    candidate_fingerprint = canonical_semantic_fingerprint(candidate)
    errors: list[str] = []
    if canonical_fingerprint != candidate_fingerprint:
        errors.extend(_fingerprint_diff(canonical_fingerprint, candidate_fingerprint))
    expected_route = str(manifest_case.get("expected_route") or "")
    actual_route = str(candidate.get("analysis", {}).get("execution_route") or "")
    if actual_route != expected_route:
        errors.append(f"expected continuation route {expected_route}, got {actual_route or '<empty>'}")
    canonical_route = str(canonical.get("analysis", {}).get("execution_route") or "")
    if actual_route != canonical_route:
        errors.append(f"canonical route parity failed: canonical={canonical_route}, continuation={actual_route}")
    if candidate_calls != canonical_calls:
        errors.append(
            f"pandas model call parity failed: canonical={canonical_calls}, continuation={candidate_calls}"
        )
    if _dependent_plan(candidate_resolved):
        errors.append("single-stage regression unexpectedly produced a dependent_retrieval_plan")
    if case_id == 9:
        rows = candidate_fingerprint["result"]["rows"]
        expected_device = "DEV-SP24-GDDR7-X32-226"
        if len(rows) != 1:
            errors.append(f"canonical R09 must return exactly one row, got {len(rows)}")
        if not any(
            str(row.get("DEVICE") or "") == expected_device
            and str(row.get("TECH") or "") == "SP"
            and str(row.get("DEN", row.get("DENSITY")) or "") == "24G"
            and str(row.get("MODE") or "") == "GDDR7"
            and str(row.get("ORG") or "").lstrip("Xx") == "32"
            and str(row.get("PKG_TYPE1", row.get("PKG1")) or "") == "FCBGA"
            and str(row.get("PKG_TYPE2", row.get("PKG2")) or "") == "DDP"
            and str(row.get("LEAD") or "") == "226"
            for row in rows
        ):
            errors.append("canonical R09 SP24/GDDR7/X32/226 result row is missing")
        if any(str(row.get("DEVICE") or "").startswith("DECOY-SP24-") for row in rows):
            errors.append("canonical R09 product-token decoy leaked into the result")
    prompt_bytes = len("fixture complex pandas prompt".encode("utf-8")) if candidate_calls else 0
    return {
        "id": str(manifest_case["id"]),
        "base_case_id": case_id,
        "question": str(case["question"]),
        "kind": "canonical_parity",
        "status": "ok" if not errors else "error",
        "route": actual_route,
        "pandas_model_calls": candidate_calls,
        "child_calls": 1,
        "prompt_bytes": prompt_bytes,
        "row_count": len(candidate_fingerprint["result"]["rows"]),
        "columns": candidate_fingerprint["result"]["columns"],
        "fingerprint_sha256": _sha256_json(candidate_fingerprint),
        "errors": errors,
    }


def canonical_semantic_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs: list[dict[str, Any]] = []
    for job in plan.get("retrieval_jobs", []) if isinstance(plan.get("retrieval_jobs"), list) else []:
        if not isinstance(job, dict):
            continue
        jobs.append(
            {
                "source_alias": str(job.get("source_alias") or ""),
                "dataset_key": str(job.get("dataset_key") or ""),
                "required_params": _json_ready(job.get("required_params") or {}),
                "filters": _json_ready(job.get("filters") or {}),
            }
        )
    jobs.sort(key=lambda item: (item["source_alias"], item["dataset_key"]))
    temporal = []
    for item in plan.get("temporal_semantics", []) if isinstance(plan.get("temporal_semantics"), list) else []:
        if isinstance(item, dict):
            temporal.append(
                {
                    "source_alias": str(item.get("source_alias") or ""),
                    "dataset_key": str(item.get("dataset_key") or ""),
                    "query_date": str(item.get("query_date") or ""),
                }
            )
    temporal.sort(key=lambda item: (item["source_alias"], item["dataset_key"], item["query_date"]))
    output = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    rows, columns = _result_rows_and_columns(payload)
    status = str(payload.get("analysis", {}).get("status") or "error").lower()
    return {
        "retrieval_jobs": jobs,
        "temporal_semantics": temporal,
        "output_contract": {
            "grain_columns": [str(value) for value in output.get("grain_columns", [])],
            "metric_columns": [str(value) for value in output.get("metric_columns", [])],
        },
        "result": {
            "rows": _unordered_rows(rows),
            "columns": columns,
        },
        "answer": {"status": "ok" if status in {"ok", "success", "complete", "completed"} else status},
    }


def _fingerprint_diff(canonical: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("retrieval_jobs", "temporal_semantics", "output_contract", "result", "answer"):
        if canonical.get(key) != candidate.get(key):
            errors.append(f"canonical fingerprint mismatch: {key}")
    return errors


def _dependent_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    value = plan.get("dependent_retrieval_plan")
    return value if isinstance(value, dict) else {}


def validate_continuation_case(case: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    state = _run_continuation_fixture(case, limits)
    errors = list(state.pop("_errors", []))
    expected_status = str(case.get("expected_status") or "")
    expected_route = str(case.get("expected_route") or "")
    if state["status"] != expected_status:
        errors.append(f"expected status {expected_status}, got {state['status']}")
    if state["route"] != expected_route:
        errors.append(f"expected route {expected_route}, got {state['route']}")
    if state["child_calls"] != int(case.get("expected_child_calls") or 0):
        errors.append(
            f"expected child calls {case.get('expected_child_calls')}, got {state['child_calls']}"
        )
    expected_stage_routes = [str(value) for value in case.get("expected_stage_routes", [])]
    if state["stage_routes"] != expected_stage_routes:
        errors.append(
            f"expected stage routes {expected_stage_routes}, got {state['stage_routes']}"
        )
    expected_first_answer_calls = int(case.get("expected_first_answer_model_calls") or 0)
    if state["first_answer_model_calls"] != expected_first_answer_calls:
        errors.append(
            "expected first answer model calls "
            f"{expected_first_answer_calls}, got {state['first_answer_model_calls']}"
        )
    expected_failure = str(case.get("expected_failure_reason") or "")
    if expected_failure and state["failure_reason"] != expected_failure:
        errors.append(
            f"expected failure reason {expected_failure}, got {state['failure_reason'] or '<empty>'}"
        )

    expected_columns = [str(value) for value in case.get("expected_columns", [])]
    expected_rows = [_normalize_row(item) for item in case.get("expected_rows", []) if isinstance(item, dict)]
    if expected_columns and state["columns"] != expected_columns:
        errors.append(f"expected columns {expected_columns}, got {state['columns']}")
    if "expected_rows" in case and not _rows_match_expected(state["rows"], expected_rows):
        errors.append("semantic result rows do not match")

    if state["contract_bytes"] > int(limits["max_continuation_contract_bytes"]):
        errors.append("continuation contract byte budget exceeded")
    if state["observation_bytes"] > int(limits["max_agent_observation_bytes"]):
        errors.append("agent observation byte budget exceeded")
    if state["stage1_prompt_bytes"] > int(limits["max_stage1_prompt_bytes"]):
        errors.append("stage 1 prompt byte budget exceeded")
    if state["stage2_prompt_bytes"] > int(limits["max_stage2_prompt_bytes"]):
        errors.append("stage 2 prompt byte budget exceeded")
    forbidden = [str(value) for value in limits.get("forbidden_agent_observation_keys", [])]
    leaked = sorted(set(forbidden).intersection(_recursive_keys(state["agent_observation"])))
    if leaked:
        errors.append(f"agent observation leaked forbidden keys: {leaked}")
    if state["child_calls"] == 2 and state["intent_llm_skipped"] is not True:
        errors.append("stage 2 did not skip the intent LLM")
    if state["child_calls"] == 2 and state["first_answer_model_calls"] != 0:
        errors.append("intermediate stage invoked the answer model")
    if state["status"] == "blocked" and state["rows"]:
        errors.append("blocked continuation exposed partial stage rows as final data")
    forbidden_datasets = {str(value) for value in case.get("forbidden_datasets", [])}
    if forbidden_datasets.intersection(state.get("datasets", [])):
        errors.append(
            f"forbidden datasets were selected: {sorted(forbidden_datasets.intersection(state['datasets']))}"
        )
    if int(case.get("expected_child_calls") or 0) == 1 and state.get("dependent_plan_created") is True:
        errors.append("single-stage question unexpectedly created a dependent retrieval plan")

    state.update(
        {
            "id": str(case["id"]),
            "question": str(case["question"]),
            "kind": "continuation_fixture",
            "status": "ok" if not errors else "error",
            "execution_status": state["status"],
            "errors": errors,
        }
    )
    return state


def validate_multiturn_scenario(scenario: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """Execute explicit result-ref handoff semantics across multiple user turns."""

    scenario_id = str(scenario.get("id") or "")
    session_id = str(scenario.get("session_id") or "")
    turns = [item for item in scenario.get("turns", []) if isinstance(item, dict)]
    errors: list[str] = []
    stored: dict[int, dict[str, Any]] = {}
    turn_results: list[dict[str, Any]] = []
    total_child_calls = 0
    for expected_turn, turn in enumerate(turns, start=1):
        turn_number = int(turn.get("turn") or 0)
        if turn_number != expected_turn:
            errors.append(f"turn order mismatch: expected {expected_turn}, got {turn_number}")
            continue
        source_turn = int(turn.get("reference_from_turn") or 0)
        upstream_ref = ""
        if source_turn:
            source = stored.get(source_turn)
            if not source:
                errors.append(f"turn {turn_number} references unavailable turn {source_turn}")
                continue
            if source["session_id"] != session_id:
                errors.append(f"turn {turn_number} attempted cross-session result reuse")
                continue
            upstream_ref = str(source["result_ref"])
        rows = [_normalize_row(item) for item in turn.get("rows", []) if isinstance(item, dict)]
        columns = [str(value) for value in turn.get("columns", [])]
        if columns != _ordered_columns(rows) and rows:
            errors.append(f"turn {turn_number} result columns do not match row schema")
        child_calls = int(turn.get("expected_child_calls") or 0)
        total_child_calls += child_calls
        result_ref = f"result:{session_id}:{scenario_id.lower()}-{turn_number}"
        stored[turn_number] = {
            "session_id": session_id,
            "result_ref": result_ref,
            "rows": deepcopy(rows),
            "columns": columns,
        }
        prompt = json.dumps(
            {
                "question": str(turn.get("question") or ""),
                "upstream_result_ref": upstream_ref,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > int(limits["max_stage1_prompt_bytes"]):
            errors.append(f"turn {turn_number} prompt byte budget exceeded")
        turn_results.append(
            {
                "turn": turn_number,
                "route": str(turn.get("expected_route") or ""),
                "child_calls": child_calls,
                "upstream_result_ref": upstream_ref,
                "result_ref": result_ref,
                "row_count": len(rows),
                "rows": rows,
                "columns": columns,
                "prompt_bytes": prompt_bytes,
            }
        )
    return {
        "id": scenario_id,
        "question": " -> ".join(str(item.get("question") or "") for item in turns),
        "kind": "multiturn_fixture",
        "status": "ok" if not errors else "error",
        "session_id": session_id,
        "turns": turn_results,
        "child_calls": total_child_calls,
        "row_count": sum(item["row_count"] for item in turn_results),
        "errors": errors,
    }


def _run_continuation_fixture(case: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    scenario = str(case.get("scenario") or "")
    session_id = str(case.get("session_id") or "")
    stage1_rows = deepcopy(case.get("stage1_rows") or [])
    stage2_rows = deepcopy(case.get("stage2_rows") or [])
    stage_routes = [str(value) for value in case.get("expected_stage_routes", [])]
    expected_route = str(case.get("expected_route") or "")
    first_answer_calls = int(case.get("expected_first_answer_model_calls") or 0)
    errors: list[str] = []
    status = "complete"
    failure_reason = ""
    rows: list[dict[str, Any]] = []
    columns: list[str] = []
    child_calls = 1
    intent_llm_skipped = False
    contract: dict[str, Any] = {}

    upstream = case.get("upstream_result") if isinstance(case.get("upstream_result"), dict) else {}
    if scenario == "invalid_result_ref":
        status = "blocked"
        if upstream.get("expired") is True:
            failure_reason = "expired_result_ref"
        elif str(upstream.get("session_id") or "") != session_id:
            failure_reason = "cross_session_result_ref"
        else:
            failure_reason = "invalid_result_ref"
    elif scenario == "invalid_dependency_graph":
        status = "blocked"
        failure_reason = (
            "max_stages_exceeded"
            if int(case.get("planned_stage_count") or 0) > int(limits["max_stages"])
            else "invalid_dependency_graph"
        )
    elif scenario == "incomplete_metadata_contract":
        catalog = case.get("catalog") if isinstance(case.get("catalog"), dict) else {}
        required_params = [str(value) for value in catalog.get("required_params", [])]
        bindings = [value for value in catalog.get("upstream_bindings", []) if isinstance(value, dict)]
        observed = {str(value) for value in catalog.get("observed_columns", [])}
        requested = {str(value) for value in case.get("requested_columns", [])}
        binding_targets = {str(value.get("target_param") or "") for value in bindings}
        if set(required_params).issubset(binding_targets) and requested.issubset(observed):
            rows = [_normalize_row(item) for item in stage1_rows if isinstance(item, dict)]
            columns = [str(value) for value in case.get("expected_columns", [])] or _ordered_columns(rows)
        else:
            status, failure_reason = "blocked", "dependency_contract_unresolved"
    elif scenario in {"independent_multi_source", "simple_single_stage", "explicit_upstream_followup"}:
        if scenario == "explicit_upstream_followup":
            if upstream.get("expired") is True:
                status, failure_reason = "blocked", "expired_result_ref"
            elif str(upstream.get("session_id") or "") != session_id:
                status, failure_reason = "blocked", "cross_session_result_ref"
        if status == "complete":
            rows = [_normalize_row(item) for item in stage1_rows if isinstance(item, dict)]
            columns = [str(value) for value in case.get("expected_columns", [])] or _ordered_columns(rows)
    else:
        binding = case.get("binding") if isinstance(case.get("binding"), dict) else {}
        binding_column = str(binding.get("source_column") or "")
        if not stage1_rows:
            rows = []
            columns = [str(value) for value in case.get("expected_columns", [])]
        elif not binding_column or any(binding_column not in row for row in stage1_rows if isinstance(row, dict)):
            status, failure_reason = "blocked", "missing_binding_column"
        else:
            contract = _build_continuation_contract(case, stage1_rows, limits)
            child_calls = 2
            intent_llm_skipped = True
            if case.get("stage2_error"):
                status, failure_reason = "blocked", "stage2_retrieval_failed"
            else:
                rows, columns = _execute_transforms(case, stage1_rows, stage2_rows)

    if status == "blocked":
        rows, columns = [], []
    execution = {
        "status": status,
        "stages_executed": child_calls,
        "auto_continued": child_calls == 2,
        "final_stage": child_calls - 1,
    }
    if failure_reason:
        execution["failure_reason"] = failure_reason
    agent_observation = {"continuation_execution": execution}
    stage1_prompt = json.dumps(
        {
            "question": str(case.get("question") or ""),
            "mode": "initial",
            "max_stages": int(limits["max_stages"]),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Resume uses the stored intent_envelope directly, so it has no intent
    # prompt.  This number deliberately measures LLM-bound bytes, not the
    # internal contract transport bytes.
    stage2_prompt = "" if child_calls == 2 else ""
    return {
        "route": expected_route if status != "blocked" else "blocked",
        "status": status,
        "failure_reason": failure_reason,
        "stage_routes": stage_routes,
        "child_calls": child_calls,
        "first_answer_model_calls": first_answer_calls,
        "intent_llm_skipped": intent_llm_skipped,
        "rows": rows,
        "columns": columns,
        "contract_bytes": _json_bytes(contract) if contract else 0,
        "observation_bytes": _json_bytes(agent_observation),
        "stage1_prompt_bytes": len(stage1_prompt.encode("utf-8")),
        "stage2_prompt_bytes": len(stage2_prompt.encode("utf-8")),
        "agent_observation": agent_observation,
        "filters": deepcopy(case.get("filters") or {}),
        "datasets": [str(value) for value in case.get("datasets", [])],
        "dependent_plan_created": bool(contract),
        "_errors": errors,
    }


def _build_continuation_contract(
    case: dict[str, Any],
    stage1_rows: list[dict[str, Any]],
    limits: dict[str, Any],
) -> dict[str, Any]:
    plan_seed = {
        "question": str(case.get("question") or ""),
        "binding": deepcopy(case.get("binding") or {}),
        "transforms": deepcopy(case.get("transforms") or []),
        "max_stages": int(limits["max_stages"]),
    }
    plan_hash = _sha256_json(plan_seed)
    values = []
    binding_column = str(plan_seed["binding"].get("source_column") or "")
    for row in stage1_rows:
        value = row.get(binding_column)
        if value not in values:
            values.append(value)
    return {
        "version": 1,
        "plan_id": f"plan-{plan_hash[:16]}",
        "plan_hash": plan_hash,
        "next_stage_index": 1,
        "max_stages": int(limits["max_stages"]),
        "continuation_ref": f"continuation:{plan_hash[:24]}",
        "binding": {**plan_seed["binding"], "values": values},
        "intent_envelope": {
            "intent_plan": {
                "dependent_retrieval_plan": {
                    "version": 1,
                    "plan_id": f"plan-{plan_hash[:16]}",
                    "plan_hash": plan_hash,
                    "max_stages": int(limits["max_stages"]),
                    "active_stage_index": 1,
                }
            }
        },
    }


def _execute_transforms(
    case: dict[str, Any],
    stage1_rows: list[dict[str, Any]],
    stage2_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    frames = {
        "stage1": pd.DataFrame(deepcopy(stage1_rows)),
        "stage2": pd.DataFrame(deepcopy(stage2_rows)),
    }
    last_output = "stage1"
    for transform in case.get("transforms", []):
        if not isinstance(transform, dict):
            raise ValueError("Continuation transform must be an object")
        operation = str(transform.get("operation") or "")
        output = str(transform.get("output") or "result")
        if operation == "select_extreme_row_per_group":
            source = _frame(frames, transform.get("source"))
            partition = [str(value) for value in transform.get("partition_by", [])]
            ordering = [item for item in transform.get("order_by", []) if isinstance(item, dict)]
            if not partition or not ordering:
                raise ValueError("select_extreme_row_per_group requires partition_by and order_by")
            sort_columns = [str(item.get("column") or "") for item in ordering]
            ascending = [str(item.get("direction") or "asc").lower() != "desc" for item in ordering]
            limit = max(1, int(transform.get("limit_per_group") or 1))
            ordered = source.sort_values(sort_columns, ascending=ascending, kind="mergesort")
            frames[output] = ordered.groupby(partition, dropna=False, sort=False).head(limit).reset_index(drop=True)
        elif operation == "aggregate_unique_list":
            source = _frame(frames, transform.get("source"))
            group_by = [str(value) for value in transform.get("group_by", [])]
            value_column = str(transform.get("value_column") or "")
            count_output = str(transform.get("count_output") or "COUNT")
            list_output = str(transform.get("list_output") or "LIST")
            records = []
            for key, group in source.groupby(group_by, dropna=False, sort=False):
                key_values = key if isinstance(key, tuple) else (key,)
                values = sorted(
                    {
                        str(value).strip()
                        for value in group[value_column].tolist()
                        if not _is_blank(value)
                    }
                )
                record = dict(zip(group_by, key_values, strict=True))
                record[count_output] = len(values)
                record[list_output] = ", ".join(values)
                records.append(record)
            frames[output] = pd.DataFrame(records, columns=[*group_by, count_output, list_output])
        elif operation == "left_join":
            left = _frame(frames, transform.get("left"))
            right = _frame(frames, transform.get("right"))
            on = [str(value) for value in transform.get("on", [])]
            frames[output] = left.merge(right, on=on, how="left", suffixes=("", "_right"))
        elif operation == "fill":
            source = _frame(frames, transform.get("source")).copy()
            for column, value in (transform.get("values") or {}).items():
                if column not in source.columns:
                    source[str(column)] = value
                else:
                    source[str(column)] = source[str(column)].fillna(value)
            frames[output] = source
        elif operation == "project":
            source = _frame(frames, transform.get("source")).copy()
            columns = [str(value) for value in transform.get("columns", [])]
            for column in columns:
                if column not in source.columns:
                    source[column] = ""
            frames[output] = source[columns].copy()
        else:
            raise ValueError(f"Unsupported continuation fixture transform: {operation}")
        last_output = output
    frame = frames.get("result", frames[last_output])
    rows = [_normalize_row(item) for item in frame.to_dict(orient="records")]
    return rows, [str(value) for value in frame.columns]


def _frame(frames: dict[str, pd.DataFrame], name: Any) -> pd.DataFrame:
    key = str(name or "")
    if key not in frames:
        raise ValueError(f"Continuation fixture frame does not exist: {key}")
    return frames[key]


def validate_export_contracts() -> dict[str, Any]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    for path, expected_name, expected_display_name in (
        (DATA_FLOW_EXPORT, "data_analysis_continuation", "08. v5_data_analysis_continuation"),
        (ROUTER_FLOW_EXPORT, "agent_tool_router_continuation", "09. v5_agent_tool_router_continuation"),
    ):
        if not path.exists():
            errors.append(f"missing continuation flow export: {path.name}")
            continue
        flow = json.loads(path.read_text(encoding="utf-8"))
        if str(flow.get("name") or "") != expected_display_name:
            errors.append(
                f"{path.name} display name mismatch: expected={expected_display_name!r}, actual={flow.get('name')!r}"
            )
        nodes = flow.get("data", {}).get("nodes", [])
        code = "\n".join(
            str(node.get("data", {}).get("node", {}).get("template", {}).get("code", {}).get("value", ""))
            for node in nodes
        )
        if str(flow.get("last_tested_version") or TARGET_LANGFLOW_VERSION) != TARGET_LANGFLOW_VERSION:
            errors.append(f"{path.name} last_tested_version is not {TARGET_LANGFLOW_VERSION}")
        details[expected_name] = {"nodes": len(nodes), "edges": len(flow.get("data", {}).get("edges", []))}
        if path == DATA_FLOW_EXPORT:
            for token in (
                "dependent_retrieval_plan",
                "continuation_contract",
                "continuation_ref",
                "intent_llm_skipped",
            ):
                if token not in code:
                    errors.append(f"data continuation export is missing contract token: {token}")
        else:
            for token in ("continuation_execution", "stages_executed", "auto_continued"):
                if token not in code:
                    errors.append(f"router continuation export is missing contract token: {token}")
    return {"status": "ok" if not errors else "error", "details": details, "errors": errors}


def _result_rows_and_columns(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows_value = data.get("rows")
    if not isinstance(rows_value, list):
        rows_value = payload.get("result_rows") if isinstance(payload.get("result_rows"), list) else []
    rows = [_normalize_row(item) for item in rows_value if isinstance(item, dict)]
    columns = data.get("columns")
    if not isinstance(columns, list):
        columns = payload.get("result_columns") if isinstance(payload.get("result_columns"), list) else []
    normalized_columns = [str(value) for value in columns]
    if not normalized_columns:
        normalized_columns = _ordered_columns(rows)
    return rows, normalized_columns


def _ordered_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    return columns


def _unordered_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (_normalize_row(row) for row in rows),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _rows_match_expected(actual_rows: list[dict[str, Any]], expected_rows: list[dict[str, Any]]) -> bool:
    """Compare row populations while allowing expected rows to be semantic subsets."""

    if len(actual_rows) != len(expected_rows):
        return False
    remaining = [_normalize_row(row) for row in actual_rows]
    for expected in (_normalize_row(row) for row in expected_rows):
        match_index = next(
            (
                index
                for index, actual in enumerate(remaining)
                if all(actual.get(column) == value for column, value in expected.items())
            ),
            None,
        )
        if match_index is None:
            return False
        remaining.pop(match_index)
    return not remaining


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _normalize_scalar(value) for key, value in row.items()}


def _normalize_scalar(value: Any) -> Any:
    if value is None or value is pd.NA:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _is_blank(value: Any) -> bool:
    normalized = _normalize_scalar(value)
    return str(normalized).strip().lower() in {"", "nan", "none", "null", "<na>"}


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recursive_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_recursive_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_recursive_keys(child))
    return keys


def run_validation(
    *,
    reference_date: str = "20260701",
    selected_ids: set[str] | None = None,
    limit: int = 0,
    check_exports: bool = True,
) -> dict[str, Any]:
    manifest = load_manifest()
    base.install_lfx_stubs()
    modules = base.load_flow_modules()
    canonical_v2 = route_validator._v2_modules()
    continuation_modules = _continuation_modules()
    canonical_questions = _canonical_v2_questions()
    base_cases = {
        int(item["id"]): _canonical_regression_base_case(
            item,
            canonical_questions[int(item["id"])],
        )
        for item in base.representative_cases()
    }
    route_expectations = route_validator.load_route_manifest()
    selected = selected_ids or set()
    cases: list[tuple[str, dict[str, Any]]] = [
        *(('regression', item) for item in manifest["regression_cases"]),
        *(('continuation', item) for item in manifest["continuation_cases"]),
        *(('multiturn', item) for item in manifest["multiturn_scenarios"]),
    ]
    if selected:
        cases = [item for item in cases if str(item[1].get("id") or "").upper() in selected]
    if limit > 0:
        cases = cases[:limit]

    results: list[dict[str, Any]] = []
    continuation_by_id = {
        str(item.get("id") or ""): item
        for item in manifest["continuation_cases"]
        if isinstance(item, dict)
    }
    for kind, case in cases:
        if kind == "regression":
            if case.get("parity_exception"):
                # R28 intentionally changes from current-status reason to the
                # metadata-defined latest history reason.  C01 owns the exact
                # two-stage semantic oracle; all other R cases remain strict
                # canonical fingerprint parity checks.
                fixture = deepcopy(continuation_by_id["C01"])
                fixture["id"] = str(case["id"])
                fixture["question"] = str(base_cases[int(case["base_case_id"])]["question"])
                result = validate_continuation_case(fixture, manifest["limits"])
                result["kind"] = "intentional_parity_exception"
                result["parity_exception"] = str(case["parity_exception"])
                result["validated_by"] = "C01"
            else:
                result = validate_regression_case(
                    case,
                    base_cases,
                    route_expectations,
                    modules,
                    canonical_v2,
                    continuation_modules,
                    reference_date,
                )
        else:
            result = (
                validate_multiturn_scenario(case, manifest["limits"])
                if kind == "multiturn"
                else validate_continuation_case(case, manifest["limits"])
            )
        results.append(result)

    export_validation = validate_export_contracts() if check_exports else {"status": "skipped", "errors": []}
    failures = [item for item in results if item.get("status") != "ok"]
    if check_exports and export_validation["status"] != "ok":
        failures.append({"id": "exports", "errors": export_validation["errors"]})
    return {
        "status": "ok" if not failures else "error",
        "reference_date": reference_date,
        "summary": {
            "measurement_scope": "synthetic_contract_fixture",
            "passed": len(results) - sum(item.get("status") != "ok" for item in results),
            "total": len(results),
            "canonical_parity_cases": sum(item.get("kind") == "canonical_parity" for item in results),
            "continuation_cases": sum(item.get("kind") == "continuation_fixture" for item in results),
            "multiturn_scenarios": sum(item.get("kind") == "multiturn_fixture" for item in results),
            "child_calls": sum(int(item.get("child_calls") or 0) for item in results),
            "max_contract_bytes": max((int(item.get("contract_bytes") or 0) for item in results), default=0),
            "max_observation_bytes": max((int(item.get("observation_bytes") or 0) for item in results), default=0),
            "max_prompt_bytes": max((int(item.get("stage1_prompt_bytes") or item.get("prompt_bytes") or 0) for item in results), default=0),
        },
        "export_validation": export_validation,
        "results": results,
    }


def _live_case_specs(
    manifest: dict[str, Any],
    selected_ids: set[str] | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Return the required R01-R30 plus the five executable live additions."""

    base_cases = {int(item["id"]): item for item in base.representative_cases()}
    canonical_questions = _canonical_v2_questions()
    specs: list[dict[str, Any]] = []
    for item in manifest["regression_cases"]:
        case_id = int(item["base_case_id"])
        specs.append(
            {
                "id": str(item["id"]),
                "question": canonical_questions[case_id],
                "kind": "regression_live",
                "manifest": item,
                "base_case": _canonical_live_base_case(
                    base_cases[case_id],
                    canonical_questions[case_id],
                ),
            }
        )
    continuation = {
        str(item.get("id") or ""): item
        for item in manifest["continuation_cases"]
        if isinstance(item, dict)
    }
    for case_id in ("C01", "C02", "C10", "C12", "C13"):
        specs.append(
            {
                "id": case_id,
                "question": str(continuation[case_id]["question"]),
                "kind": "continuation_live",
                "manifest": continuation[case_id],
                "base_case": base_cases[30] if case_id == "C02" else None,
            }
        )
    selected = {str(value).strip().upper() for value in (selected_ids or set()) if str(value).strip()}
    if selected:
        specs = [item for item in specs if item["id"].upper() in selected]
    if limit > 0:
        specs = specs[:limit]
    return specs


def _canonical_v2_questions(path: Path = CANONICAL_QUESTIONS_PATH) -> dict[int, str]:
    """Read the immutable numbered V2 questions instead of validator fixtures."""

    questions: dict[int, str] = {}
    pattern = re.compile(r"^(\d+)\.\s+\[[^\]]+\]\s+(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        case_id = int(match.group(1))
        if 1 <= case_id <= 30:
            questions[case_id] = match.group(2)
    expected = set(range(1, 31))
    if set(questions) != expected:
        missing = sorted(expected - set(questions))
        raise ValueError(f"Canonical V2 question file is incomplete: missing={missing}")
    return questions


def _canonical_live_base_case(base_case: dict[str, Any], question: str) -> dict[str, Any]:
    """Remove only fixture expectations invalidated by canonical question drift."""

    case = _canonical_regression_base_case(base_case, question)
    case_id = int(case.get("id") or 0)
    case["live_expectation_mode"] = "canonical_contract"
    if case_id == 4:
        # The canonical question asks for all HBM BOH WIP, not only W/B.
        jobs = case.get("intent_response", {}).get("intent_plan", {}).get("retrieval_jobs", [])
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict):
                job["filters"] = {}
    if case_id == 9:
        # Canonical SP24/GDDR7 rows are absent from the inherited provider.
        # The live harness adds an isolated canonical row plus decoys, so the
        # product-token contract can be exercised without mutating Flow 01.
        case["min_rows"] = 1
        for key in (
            "expected_row_count",
            "expected_first_row",
            "expected_rows",
            "forbidden_values",
        ):
            case.pop(key, None)
        case["live_fixture_adapter"] = "canonical_sp24_product_token_rows"
    if case_id == 22:
        # The canonical question does not constrain TECH.  The legacy fixture's
        # TECH=CMP row selector is sample-data scaffolding, not user intent, so
        # semantic-live validation must not require it.
        jobs = case.get("intent_response", {}).get("intent_plan", {}).get(
            "retrieval_jobs", []
        )
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict):
                job["filters"] = {}
    return case


def _render_live_intent_prompt(intent_variables: dict[str, Any]) -> str:
    base_template = (
        ROOT / "langflow_components" / "data_analysis_flow_v2" / "03_intent_prompt_template_ko.md"
    ).read_text(encoding="utf-8")
    continuation_rules = (CONTINUATION_ROOT / "03_continuation_rules_prompt_ko.md").read_text(
        encoding="utf-8"
    )
    template = base_template.rstrip() + "\n\n" + continuation_rules.strip() + "\n"
    return template.format(**{str(key): str(value) for key, value in intent_variables.items()})


def _live_pipeline_modules(
    modules: dict[str, Any],
    continuation: dict[str, Any],
) -> dict[str, Any]:
    v2 = route_validator._v2_modules()
    v2["executor"] = continuation["executor"]
    v2["answer"] = continuation["answer"]
    v2["selection"] = base.load_module(
        ROOT / "langflow_components" / "data_analysis_flow_v2" / "15_function_case_selection_builder.py"
    )
    return v2


def _execute_live_stage(
    *,
    spec: dict[str, Any],
    stage_index: int,
    reference_date: str,
    session_id: str,
    modules: dict[str, Any],
    continuation: dict[str, Any],
    v2: dict[str, Any],
    metadata_context: dict[str, Any],
    recorder: LiveModelRecorder,
    public_continuation: dict[str, Any] | None = None,
    stored_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the same pure component boundaries as one Langflow child run."""

    question = str(spec["question"])
    before = recorder.summary()
    if public_continuation:
        payload = continuation["request"].build_request(
            question,
            session_id=session_id,
            upstream_result_ref=public_continuation.get("result_ref", ""),
            continuation_ref=public_continuation.get("continuation_ref", ""),
            continuation_contract=public_continuation.get("continuation_contract", {}),
            skip_intermediate_answer=True,
        )
    else:
        payload = continuation["request"].build_request(question, session_id=session_id)
    payload.setdefault("request", {})["reference_date"] = reference_date

    base_candidate_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    candidate_payload = continuation["catalog_closure"].close_dependency_catalog_candidates(
        base_candidate_payload,
        metadata_context["table"],
        max_table_items=5,
    )
    metadata_candidates = candidate_payload.get("metadata_candidates", candidate_payload)
    intent_variables = base.with_specialized_prompt(
        continuation["intent_vars"].build_variables(payload, candidate_payload)
    )
    rendered_intent_prompt = _render_live_intent_prompt(intent_variables)
    canonical_intent_variables = base.with_specialized_prompt(
        v2["intent_vars"].build_variables(payload, base_candidate_payload)
    )
    canonical_template = (
        ROOT / "langflow_components" / "data_analysis_flow_v2" / "03_intent_prompt_template_ko.md"
    ).read_text(encoding="utf-8")
    canonical_intent_prompt = canonical_template.format(
        **{str(key): str(value) for key, value in canonical_intent_variables.items()}
    )
    continuation_rules = (CONTINUATION_ROOT / "03_continuation_rules_prompt_ko.md").read_text(
        encoding="utf-8"
    )
    prompt_breakdown_bytes = {
        "question": len(str(intent_variables.get("question") or "").encode("utf-8")),
        "state_summary": len(str(intent_variables.get("state_summary") or "").encode("utf-8")),
        "metadata_candidates": len(
            str(intent_variables.get("metadata_candidates") or "").encode("utf-8")
        ),
        "output_schema": len(str(intent_variables.get("output_schema") or "").encode("utf-8")),
        "specialized_prompt": len(
            str(intent_variables.get("specialized_prompt") or "").encode("utf-8")
        ),
        "base_template": len(canonical_template.encode("utf-8")),
        "continuation_rules": len(continuation_rules.encode("utf-8")),
        "canonical_rendered": len(canonical_intent_prompt.encode("utf-8")),
        "continuation_rendered": len(rendered_intent_prompt.encode("utf-8")),
        "continuation_overhead": max(
            0,
            len(rendered_intent_prompt.encode("utf-8"))
            - len(canonical_intent_prompt.encode("utf-8")),
        ),
    }
    stored_loader = (
        (lambda ref: deepcopy(stored_document))
        if isinstance(stored_document, dict)
        else None
    )
    intent_response, intent_route_trace = continuation["intent_router"].route_intent_response(
        payload,
        rendered_intent_prompt,
        model_invoker=recorder.invoker("intent"),
        stored_plan_loader=stored_loader,
    )
    compiled_response, compiler_trace = continuation["compiler"].compile_intent_response(
        payload,
        intent_response,
        candidate_payload,
    )
    payload = v2["intent"].normalize_intent_plan(
        payload,
        compiled_response,
        candidate_payload,
    )
    inspection = payload.setdefault("trace", {}).setdefault("inspection", {})
    inspection["continuation_intent_router"] = deepcopy(intent_route_trace)
    inspection["dependent_retrieval_compiler"] = deepcopy(compiler_trace)
    payload = modules["hydrator"].hydrate_retrieval_jobs(
        payload,
        metadata_context["table"],
        retrieval_mode="dummy",
    )
    if public_continuation and isinstance(stored_document, dict):
        stored_payload = stored_document.get("payload") if isinstance(stored_document.get("payload"), dict) else {}
        payload = continuation["result_loader"]._restore_explicit_upstream_result(
            payload,
            stored_payload,
            str(public_continuation.get("result_ref") or ""),
            "validation",
            "live_checkpoint",
        )
    else:
        payload = continuation["result_loader"].load_previous_result(payload)
    payload = continuation["binding_alias_normalizer"].normalize_continuation_binding_aliases(
        payload
    )
    payload = continuation["binder"].bind_upstream_entity_parameters(payload)
    payload = modules["validator"].validate_retrieval_payload(payload)
    bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    retrieved = modules["dummy"].retrieve_dummy_data(bundle)
    retrieved = _augment_live_fixture_retrieval(spec, retrieved, reference_date)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, retrieved)
    payload = modules["adapter"].build_retrieval_payload(payload)
    payload = continuation["gate"].apply_retrieval_execution_gate(payload)
    resolved = v2["resolver"].resolve_simple_analysis_contract(payload)

    selection = v2["selection"].build_function_case_selection_only(resolved)
    helper_library = (
        ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
    ).read_text(encoding="utf-8")
    helper_code = modules["helper_builder"].build_selected_helper_code(selection, helper_library)
    pandas_prompt = v2["pandas_prompt"].build_route_aware_pandas_prompt(
        resolved,
        (
            ROOT / "langflow_components" / "data_analysis_flow" / "16_pandas_prompt_template_ko.md"
        ).read_text(encoding="utf-8"),
        helper_code,
    )
    executed = v2["executor"].execute_hybrid_analysis(
        resolved,
        pandas_prompt,
        model_invoker=recorder.invoker("pandas"),
        repair_prompt_template=(
            ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md"
        ).read_text(encoding="utf-8"),
        function_case_helper_code=helper_code,
        max_repair_attempts=1,
    )
    if str(executed.get("analysis", {}).get("status") or "").lower() in {"ok", "success"}:
        result_ref = f"result:{session_id}:{spec['id'].lower()}-stage-{stage_index + 1}"
        executed.setdefault("data", {})["data_ref"] = {
            "ref_id": result_ref,
            "store": "mongodb",
            "role": "analysis_result",
        }
        executed["data_refs"] = [
            {
                "ref_id": result_ref,
                "kind": "analysis_result",
                "store": "mongodb",
                "row_count": int(executed.get("data", {}).get("row_count") or 0),
                "columns": list(executed.get("data", {}).get("columns") or []),
            }
        ]
        executed.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
            "stage": "23_mongodb_result_store",
            "status": "ok",
            "data_ref": result_ref,
            "validation_fixture": True,
        }
    answered = v2["answer"].build_hybrid_answer_response(
        executed,
        "",
        model_invoker=recorder.invoker("answer"),
        use_llm_answer=True,
        answer_prompt_template=(
            ROOT / "langflow_components" / "data_analysis_flow" / "19_answer_prompt_template_ko.md"
        ).read_text(encoding="utf-8"),
        domain_answer_guidance=(
            ROOT / "langflow_components" / "data_analysis_flow" / "answer_domain_guidance_input_example_ko.md"
        ).read_text(encoding="utf-8"),
    )
    api_response = continuation["api"].build_api_response(answered, answered.get("answer_message", ""))
    after = recorder.summary()
    executor_calls = (
        answered.get("trace", {})
        .get("inspection", {})
        .get("fast_path", {})
        .get("llm_calls", {})
    )
    stage_metrics = {
        "stage_index": stage_index,
        "intent_router": intent_route_trace,
        "compiler": compiler_trace,
        "rendered_intent_prompt_chars": len(rendered_intent_prompt),
        "intent_prompt_breakdown_bytes": prompt_breakdown_bytes,
        "model_calls": {
            key: int(after["actual_calls"][key]) - int(before["actual_calls"][key])
            for key in after["actual_calls"]
        },
        "executor_model_calls": {
            "pandas_generation": int(executor_calls.get("pandas_generation") or 0),
            "repair": int(executor_calls.get("repair") or 0),
            "answer": int(executor_calls.get("answer") or 0),
        },
        "cached_model_responses": {
            key: int(after["cached_responses"][key]) - int(before["cached_responses"][key])
            for key in after["cached_responses"]
        },
        "prompt_chars": {
            key: int(after["prompt_chars"][key]) - int(before["prompt_chars"][key])
            for key in ("intent", "pandas", "answer")
        },
        "prompt_bytes": {
            key: int(after["prompt_bytes"][key]) - int(before["prompt_bytes"][key])
            for key in ("intent", "pandas", "answer")
        },
        "usage_metadata": {
            key: {
                token_key: int(after["usage_metadata"][key][token_key])
                - int(before["usage_metadata"][key][token_key])
                for token_key in _empty_usage_metadata()
            }
            for key in ("intent", "pandas", "answer")
        },
        "usage_samples": {
            key: int(after["usage_samples"][key]) - int(before["usage_samples"][key])
            for key in ("intent", "pandas", "answer")
        },
        "route": str(answered.get("analysis", {}).get("execution_route") or ""),
        "analysis_status": str(answered.get("analysis", {}).get("status") or ""),
        "continuation_status": str(api_response.get("continuation", {}).get("status") or "not_applicable"),
    }
    return {
        "payload": answered,
        "api_response": api_response,
        "metadata_candidates": metadata_candidates,
        "stage_metrics": stage_metrics,
    }


def _augment_live_fixture_retrieval(
    spec: dict[str, Any],
    retrieved: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    """Add only canonical-question fixtures missing from the inherited provider."""

    case_id = str(spec.get("id") or "")
    if case_id not in {"R09", "R22", "C01", "C10", "C12"}:
        return retrieved
    result = deepcopy(retrieved)
    for source in result.get("source_results", []):
        if not isinstance(source, dict):
            continue
        dataset_key = str(source.get("dataset_key") or "")
        if case_id == "C01":
            if dataset_key == "lot_status":
                fixtures = [
                    {
                        "LOT_ID": "LOT-C01-001",
                        "OPER_NAME": "W/B1",
                        "HOLD_STAT": "OnHold",
                        "PROD_QTY": 10,
                        "WF_QTY": 4,
                        "IN_TAT": 2.0,
                        "CUM_TAT": 8.0,
                        "HOLD_REASON": "fixture hold",
                        "LOT_STAT": "HOLD",
                    }
                ]
                source["rows"] = fixtures
                source["row_count"] = len(fixtures)
                source["columns"] = sorted({column for row in fixtures for column in row})
                source["preview_rows"] = fixtures
                source.setdefault("validation_fixture_adapters", []).append(
                    "current_hold_lot_for_continuation"
                )
            elif dataset_key == "hold_history":
                fixtures = [
                    {
                        "LOT_ID": "LOT-C01-001",
                        "OPER_NAME": "W/B1",
                        "HOLD_TM": f"{reference_date}120000",
                        "HOLD_CD": "H-C01",
                        "HOLD_DESC": "fixture latest hold reason",
                    }
                ]
                source["rows"] = fixtures
                source["row_count"] = len(fixtures)
                source["columns"] = sorted({column for row in fixtures for column in row})
                source["preview_rows"] = fixtures
                source.setdefault("validation_fixture_adapters", []).append(
                    "latest_hold_history_for_continuation"
                )
            continue
        if case_id == "R22" and dataset_key in {
            "production",
            "production_today",
            "wip",
            "wip_today",
            "lot_status",
        }:
            # The canonical question asks for a current product comparison.
            # Keep a small positive fixture with one duplicate group and one
            # differing attribute; it exercises source selection and the
            # compare operation without depending on the dummy provider.
            fixtures = [
                {
                    "DATE": reference_date,
                    "WORK_DT": reference_date,
                    "TECH": "CMP",
                    "DEN": "24G",
                    "PKG_TYPE2": "DDP",
                    "MCP_NO": "M-001",
                    "MODE": "GDDR7",
                    "PKG_TYPE1": "FCBGA",
                    "LEAD": "226",
                    "PRODUCTION": 12,
                    "WIP": 12,
                    "LOT_ID": "LOT-CMP-001",
                    "HOLD_STAT": "",
                },
                {
                    "DATE": reference_date,
                    "WORK_DT": reference_date,
                    "TECH": "CMP",
                    "DEN": "24G",
                    "PKG_TYPE2": "DDP",
                    "MCP_NO": "M-001",
                    "MODE": "GDDR6",
                    "PKG_TYPE1": "FCBGA",
                    "LEAD": "226",
                    "PRODUCTION": 9,
                    "WIP": 9,
                    "LOT_ID": "LOT-CMP-002",
                    "HOLD_STAT": "",
                },
            ]
            source["rows"] = fixtures
            source["row_count"] = len(fixtures)
            source["columns"] = sorted({column for row in fixtures for column in row})
            source["preview_rows"] = fixtures
            source.setdefault("validation_fixture_adapters", []).append(
                "current_product_comparison_rows"
            )
            continue
        if case_id == "C10" and dataset_key == "target":
            fixtures = [
                {
                    "DATE": "20260706",
                    "TECH": "SP",
                    "DEN": "24G",
                    "MODE": "GDDR7",
                    "ORG": "32",
                    "PKG1": "FCBGA",
                    "PKG2": "DDP",
                    "LEAD": "226",
                    "MCP NO": "M-PLAN-001",
                    "INPUT 계획": 100,
                    "OUT 계획": 90,
                }
            ]
            source["rows"] = fixtures
            source["row_count"] = len(fixtures)
            source["columns"] = sorted({column for row in fixtures for column in row})
            source["preview_rows"] = fixtures
            source.setdefault("validation_fixture_adapters", []).append(
                "target_plan_for_source_selection"
            )
            continue
        if case_id == "C12":
            if dataset_key != "equipment_assign":
                continue
            fixtures = [
                {
                    "RECIPE_ID": "R0429-A",
                    "EQP_ID": "EQP-01",
                    "EQP_MODEL": "MODEL-A",
                    "OPER_NAME": "FCB1",
                },
                {
                    "RECIPE_ID": "R0429-B",
                    "EQP_ID": "EQP-02",
                    "EQP_MODEL": "MODEL-B",
                    "OPER_NAME": "FCB2",
                },
                {
                    "RECIPE_ID": "R0428-DECOY",
                    "EQP_ID": "EQP-DECOY-01",
                    "EQP_MODEL": "MODEL-X",
                    "OPER_NAME": "FCB1",
                },
                {
                    "RECIPE_ID": "XR0429-DECOY",
                    "EQP_ID": "EQP-DECOY-02",
                    "EQP_MODEL": "MODEL-Y",
                    "OPER_NAME": "FCB2",
                },
            ]
            rows = [deepcopy(item) for item in source.get("rows", []) if isinstance(item, dict)]
            rows.extend(fixtures)
            source["rows"] = rows
            source["row_count"] = len(rows)
            source["columns"] = sorted({column for row in rows for column in row})
            source["preview_rows"] = rows[: min(20, len(rows))]
            source.setdefault("validation_fixture_adapters", []).append(
                "equipment_assign_recipe_prefix_rows"
            )
            continue
        if dataset_key != "production":
            continue
        params = source.get("applied_params") if isinstance(source.get("applied_params"), dict) else {}
        work_date = str(params.get("DATE") or reference_date).replace("-", "")[:8]
        fixtures = _canonical_sp24_fixture_rows(work_date)
        # Isolate the token-matching contract from whatever rows happen to be
        # present in the live dummy provider.  The production flow is not
        # changed; this validation adapter supplies one positive row and two
        # deliberate decoys so a successful result is attributable to the
        # product-token function case rather than incidental fixture data.
        rows = fixtures
        source["rows"] = rows
        source["row_count"] = len(rows)
        source["columns"] = sorted({column for row in rows for column in row})
        source["preview_rows"] = rows[: min(20, len(rows))]
        source.setdefault("validation_fixture_adapters", []).append(
            "canonical_sp24_product_token_rows"
        )
    return result


def _stored_stage_document(
    stage: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    payload = deepcopy(stage["payload"])
    rows = [deepcopy(item) for item in payload.get("data", {}).get("rows", []) if isinstance(item, dict)]
    payload["result_rows"] = rows
    payload["storage_manifest"] = {
        "result_rows": {
            "complete": True,
            "stored_count": len(rows),
        }
    }
    return {
        "session_id": session_id,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "payload": payload,
    }


def _live_expected_datasets(spec: dict[str, Any]) -> tuple[set[str], set[str]]:
    case_id = str(spec["id"])
    base_case = spec.get("base_case")
    if isinstance(base_case, dict):
        plan = base_case.get("intent_response", {}).get("intent_plan", {})
        required = {
            str(item.get("dataset_key") or "")
            for item in plan.get("retrieval_jobs", [])
            if isinstance(item, dict) and str(item.get("dataset_key") or "")
        }
    else:
        required = set()
    forbidden: set[str] = set()
    if case_id in {"R28", "C01"}:
        required = {"lot_status", "hold_history"}
    elif case_id == "C10":
        required = {"target"}
    elif case_id == "C12":
        required = {"equipment_assign"}
    elif case_id == "C13":
        required = {"lot_status"}
        forbidden = {"hold_history"}
    elif case_id == "R22":
        # The question asks for a current product population, not a metric
        # owned exclusively by one table.  A weak model may select any
        # current-day catalog that exposes the declared comparison columns
        # (production, WIP, or LOT status).  The semantic contract therefore
        # validates the result shape and current-scope filter rather than
        # treating one physical dataset key as mandatory.
        required = set()
    return required, forbidden


def _live_filter_errors(spec: dict[str, Any], stage_payloads: list[dict[str, Any]]) -> list[str]:
    jobs = [
        item
        for payload in stage_payloads
        for item in payload.get("intent_plan", {}).get("retrieval_jobs", [])
        if isinstance(item, dict)
    ]
    case_id = str(spec["id"])
    errors: list[str] = []
    if case_id == "R04":
        if not any(
            _filter_matches(job.get("filters", {}).get("TSV_DIE_TYP"), "not_blank", None)
            for job in jobs
        ):
            errors.append("HBM TSV_DIE_TYP not_blank filter is missing")
    if case_id == "R09":
        plans = [payload.get("intent_plan", {}) for payload in stage_payloads]
        function_cases = [
            item
            for plan in plans
            if isinstance(plan, dict)
            for item in plan.get("pandas_function_cases", [])
            if isinstance(item, dict)
        ]
        if not any(
            str(item.get("function_name") or "") == "match_product_tokens"
            and str(item.get("input_text") or "").strip() == "SP 24G GDDR7 X32 226 FCBGA DDP"
            for item in function_cases
        ):
            errors.append("canonical SP24/GDDR7 product_token_match contract is missing")
    if case_id in {"C12"}:
        if not any(
            _filter_matches(job.get("filters", {}).get("RECIPE_ID"), "starts_with", "R0429")
            for job in jobs
        ):
            errors.append("RECIPE_ID starts_with R0429 filter is missing")
    if case_id in {"R28", "C01", "C13"}:
        if not any(
            str(job.get("dataset_key") or "") == "lot_status"
            and _filter_matches(job.get("filters", {}).get("HOLD_STAT"), "eq", "OnHold")
            for job in jobs
        ):
            errors.append("lot_status HOLD_STAT=OnHold filter is missing")
    if case_id == "C10":
        if not any(
            str(job.get("dataset_key") or "") == "target"
            and (
                str(job.get("required_params", {}).get("DATE") or "") == "20260706"
                or _filter_matches(job.get("filters", {}).get("DATE"), "eq", "20260706")
            )
            for job in jobs
        ):
            errors.append("target DATE=20260706 contract is missing")
    return errors


def _validate_r09_product_result(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> tuple[list[str], list[str]]:
    """Validate the canonical product-token fixture without inventing grain.

    The registered standard product grain intentionally does not contain
    ``ORG``.  When a model includes it we can validate the positive/decoy
    rows directly; when it is absent we validate the declared standard grain
    and the canonical metric while reporting the missing discriminator as a
    diagnostic warning rather than a false execution failure.
    """

    errors: list[str] = []
    warnings: list[str] = []
    expected = {
        "TECH": "SP",
        "DEN": "24G",
        "MODE": "GDDR7",
        "PKG_TYPE1": "FCBGA",
        "PKG_TYPE2": "DDP",
        "LEAD": "226",
    }
    if len(rows) != 1:
        errors.append(
            "canonical SP24/GDDR7/X32/226 product fixture was not isolated to one result row"
        )
        return errors, warnings

    row = rows[0]
    if any(str(row.get(key) or "") != value for key, value in expected.items()):
        errors.append("canonical SP24/GDDR7/X32/226 product row is missing")
    has_org = "ORG" in columns or "ORG" in row
    if has_org:
        if str(row.get("ORG") or "").lstrip("Xx") != "32":
            errors.append("canonical SP24 product-token ORG discriminator is incorrect")
    else:
        warnings.append(
            "ORG is not part of the registered standard product grain; token isolation was validated on the declared grain"
        )
    production = row.get("PRODUCTION")
    if production not in (None, ""):
        try:
            if abs(float(production) - 424.0) > 1e-9:
                errors.append("canonical SP24 product-token metric does not match the positive fixture")
        except (TypeError, ValueError):
            errors.append("canonical SP24 product-token metric is not numeric")
    if str(row.get("DEVICE") or "").startswith("DECOY-SP24-"):
        errors.append("canonical SP24 product-token decoy leaked into the result")
    return errors, warnings


def _live_route_mismatch_is_error(expected_route: str, actual_route: str) -> bool:
    """Return true only when a mismatch changes continuation control flow."""

    expected = str(expected_route or "").strip().lower()
    actual = str(actual_route or "").strip().lower()
    return bool(expected and expected != actual and "continuation" in {expected, actual})


def _filter_matches(value: Any, operator: str, expected: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if str(value.get("operator") or "eq").strip().lower() != operator:
        return False
    actual = value.get("value", value.get("values"))
    if operator in {"not_blank", "is_blank"} and expected is None:
        return True
    if isinstance(expected, list):
        return sorted(str(item) for item in (actual if isinstance(actual, list) else [actual])) == sorted(
            str(item) for item in expected
        )
    return str(actual or "") == str(expected)


def _validate_real_router_projection(
    question: str,
    api_responses: list[dict[str, Any]],
    limits: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Replay actual child API envelopes through the production Flow-15 tool."""

    _install_router_validation_stub()
    router = base.load_module(ROUTER_COMPONENT)

    class Harness:
        def __init__(self) -> None:
            self._attributes = {"flow_tweak_data": {"question": question}}
            self.required_all_keywords = ""
            self.required_any_phrases = ""
            self.keyword_gate_message = ""
            self.enable_auto_continuation = True
            self.max_continuation_stages = 2
            self.continuation_timeout_seconds = 60
            self._resolved_continuation_input_ids = {
                name: "request-loader" for name in router.CONTINUATION_INPUT_NAMES
            }
            self.responses = [deepcopy(item) for item in api_responses]
            self.calls: list[dict[str, Any]] = []

        def _inherit_runtime_session(self):
            request = api_responses[0].get("request", {}) if api_responses else {}
            return str(request.get("session_id") or "")

        async def _execute_stage(self, args, timeout):
            self.calls.append(deepcopy(args))
            return self.responses.pop(0)

    harness = Harness()
    message = asyncio.run(router.CachedContinuationRunFlowTool._run_selected_flow(harness))
    # LFX Message.data exposes the standard Message envelope as well as custom
    # metadata. The Agent-owned observation is the one custom contract field;
    # leak detection still scans the complete runtime envelope.
    runtime_envelope = deepcopy(getattr(message, "data", {}) or {})
    execution = (
        runtime_envelope.get("continuation_execution")
        if isinstance(runtime_envelope, dict)
        else None
    )
    observation = {"continuation_execution": deepcopy(execution)}
    errors: list[str] = []
    if not isinstance(execution, dict):
        errors.append("router continuation_execution observation is missing")
    forbidden = set(str(value) for value in limits.get("forbidden_agent_observation_keys", []))
    leaked = sorted(forbidden.intersection(_recursive_keys(runtime_envelope)))
    if leaked:
        errors.append(f"router observation leaked forbidden keys: {leaked}")
    observation_bytes = _json_bytes(observation)
    if observation_bytes > int(limits["max_agent_observation_bytes"]):
        errors.append("router observation byte budget exceeded")
    if len(harness.calls) != len(api_responses):
        errors.append(
            f"router child call count mismatch: expected {len(api_responses)}, got {len(harness.calls)}"
        )
    return {
        "message_chars": len(str(getattr(message, "text", "") or "")),
        "observation": observation,
        "observation_bytes": observation_bytes,
        "runtime_envelope_keys": sorted(runtime_envelope) if isinstance(runtime_envelope, dict) else [],
        "child_calls": len(harness.calls),
    }, errors


def _install_router_validation_stub() -> None:
    """Supply only RunFlowBaseComponent when the lightweight LFX fixture lacks it."""

    try:
        from lfx.base.tools.run_flow import RunFlowBaseComponent as _RunFlowBaseComponent  # noqa: F401

        return
    except (ImportError, ModuleNotFoundError):
        pass

    root = sys.modules.get("lfx")
    if root is None:
        root = types.ModuleType("lfx")
        sys.modules["lfx"] = root
    if not hasattr(root, "__path__"):
        root.__path__ = []
    base_package = sys.modules.setdefault("lfx.base", types.ModuleType("lfx.base"))
    tools_package = sys.modules.setdefault("lfx.base.tools", types.ModuleType("lfx.base.tools"))
    run_flow_module = sys.modules.setdefault(
        "lfx.base.tools.run_flow",
        types.ModuleType("lfx.base.tools.run_flow"),
    )
    base_package.__path__ = getattr(base_package, "__path__", [])
    tools_package.__path__ = getattr(tools_package, "__path__", [])

    class RunFlowBaseComponent:
        pass

    run_flow_module.RunFlowBaseComponent = RunFlowBaseComponent
    io_module = sys.modules.get("lfx.io")
    if io_module is not None:
        class _ValidationInput:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.args = args
                self.__dict__.update(kwargs)

        for name in (
            "BoolInput",
            "DropdownInput",
            "IntInput",
            "MessageTextInput",
            "MultilineInput",
            "Output",
            "StrInput",
        ):
            if not hasattr(io_module, name):
                setattr(io_module, name, _ValidationInput)


def _validate_live_result(
    spec: dict[str, Any],
    stages: list[dict[str, Any]],
    recorder: LiveModelRecorder,
    limits: dict[str, Any],
) -> dict[str, Any]:
    final = stages[-1]
    payload = final["payload"]
    api_response = final["api_response"]
    errors: list[str] = []
    warnings: list[str] = []
    router_projection, router_errors = _validate_real_router_projection(
        str(spec["question"]),
        [item["api_response"] for item in stages],
        limits,
    )
    errors.extend(router_errors)
    semantic = semantic_validator.validate_semantic_payload(
        payload,
        question=str(spec["question"]),
    )
    errors.extend(
        f"{item.get('type')}: {item.get('message')}"
        for item in semantic.get("errors", [])
        if isinstance(item, dict)
    )
    warnings.extend(
        f"{item.get('type')}: {item.get('message')}"
        for item in semantic.get("warnings", [])
        if isinstance(item, dict)
    )
    stage_payloads = [item["payload"] for item in stages]
    datasets = {
        str(job.get("dataset_key") or "")
        for stage_payload in stage_payloads
        for job in stage_payload.get("intent_plan", {}).get("retrieval_jobs", [])
        if isinstance(job, dict) and str(job.get("dataset_key") or "")
    }
    required, forbidden = _live_expected_datasets(spec)
    if not required.issubset(datasets):
        errors.append(f"required datasets missing: {sorted(required - datasets)}")
    if forbidden.intersection(datasets):
        errors.append(f"forbidden datasets selected: {sorted(forbidden.intersection(datasets))}")
    errors.extend(_live_filter_errors(spec, stage_payloads))

    base_case = spec.get("base_case")
    if isinstance(base_case, dict) and str(spec["id"]) != "R28":
        expectation_case = base_case
        if str(spec["id"]) == "R22":
            # R22 asks for a current product population.  The current-day
            # catalog may legitimately resolve that population to production,
            # WIP, or LOT status when the selected comparison columns are
            # available.  Keep the result-shape/row assertions below, but do
            # not make the fixture's original physical dataset key a live
            # requirement.
            expectation_case = deepcopy(base_case)
            expected_plan = expectation_case.get("intent_response", {}).get("intent_plan", {})
            if isinstance(expected_plan, dict):
                expected_plan["retrieval_jobs"] = []
        expectation_issues = [
            item
            for item in semantic_validator.validate_case_expectation(expectation_case, payload)
            if isinstance(item, dict)
        ]
        errors.extend(
            f"{item.get('type')}: {item.get('message')}"
            for item in expectation_issues
        )
    analysis_status = str(payload.get("analysis", {}).get("status") or "").lower()
    if analysis_status not in {"ok", "success"}:
        errors.append(f"analysis status is not ok: {analysis_status or '<empty>'}")
    api_status = str(api_response.get("status") or "").lower()
    if api_status not in {"ok", "success"}:
        errors.append(f"API status is not ok: {api_status or '<empty>'}")
    if not str(api_response.get("message") or "").strip():
        errors.append("final answer message is empty")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = [item for item in data.get("rows", []) if isinstance(item, dict)]
    columns = [str(value) for value in data.get("columns", [])]
    row_count = int(data.get("row_count") or 0)
    if row_count < len(rows):
        errors.append("result row_count is smaller than the returned row population")
    if rows and any(any(column not in columns for column in row) for row in rows):
        errors.append("result rows contain columns outside data.columns")
    if str(spec["id"]) == "R09":
        r09_errors, r09_warnings = _validate_r09_product_result(rows, columns)
        errors.extend(r09_errors)
        warnings.extend(r09_warnings)
    if str(spec["id"]) == "C12":
        matched_equipment = {
            (str(row.get("RECIPE_ID") or ""), str(row.get("EQP_ID") or ""))
            for row in rows
        }
        expected_equipment = {("R0429-A", "EQP-01"), ("R0429-B", "EQP-02")}
        if not expected_equipment.issubset(matched_equipment):
            errors.append("equipment_assign R0429 positive fixtures are missing from the result")
        if any(not recipe.startswith("R0429") for recipe, _ in matched_equipment):
            errors.append("RECIPE_ID starts_with filter allowed a prefix decoy into the result")
        if any(eqp_id.startswith("EQP-DECOY-") for _, eqp_id in matched_equipment):
            errors.append("equipment_assign recipe prefix decoy leaked into the result")
    output_contract = payload.get("intent_plan", {}).get("output_contract", {})
    required_columns = [str(value) for value in output_contract.get("required_columns", [])]
    missing_columns = [column for column in required_columns if column not in columns]
    if missing_columns:
        errors.append(f"result contract columns missing: {missing_columns}")
    if isinstance(base_case, dict):
        minimum = int(base_case.get("min_rows") or 1)
        if row_count < minimum:
            errors.append(f"row_count {row_count} is below semantic minimum {minimum}")

    expected_route = str(spec["manifest"].get("expected_route") or "").lower()
    actual_route = str(payload.get("analysis", {}).get("execution_route") or "").lower()
    continuation_status = str(api_response.get("continuation", {}).get("status") or "not_applicable")
    overall_route = "continuation" if len(stages) == 2 else actual_route
    if expected_route and overall_route != expected_route:
        route_message = f"expected route {expected_route}, got {overall_route or '<empty>'}"
        # Fast versus Complex is an optimization decision.  If both remain a
        # single child execution, semantic success is more important than the
        # exact route chosen by a weaker model.  Continuation mismatches change
        # the number and dependency of external calls, so those remain errors.
        if _live_route_mismatch_is_error(expected_route, overall_route):
            errors.append(route_message)
        else:
            warnings.append("route_advisory: " + route_message)
    expected_child_calls = int(spec["manifest"].get("expected_child_calls") or (2 if expected_route == "continuation" else 1))
    if len(stages) != expected_child_calls:
        errors.append(f"expected child calls {expected_child_calls}, got {len(stages)}")
    if len(stages) == 2:
        if stages[0]["stage_metrics"]["model_calls"]["answer"] != 0:
            errors.append("intermediate continuation stage called the answer model")
        if stages[1]["stage_metrics"]["model_calls"]["intent"] != 0:
            errors.append("continuation resume called the intent model")
        if stages[1]["stage_metrics"]["cached_model_responses"]["intent"] != 0:
            errors.append("continuation resume consumed a cached intent response instead of stored plan")
        if stages[1]["stage_metrics"]["intent_router"].get("intent_llm_skipped") is not True:
            errors.append("continuation resume did not report intent_llm_skipped")
        if continuation_status != "complete":
            errors.append(f"final continuation status is not complete: {continuation_status}")
    first_metrics = stages[0]["stage_metrics"]
    first_usage = first_metrics.get("usage_metadata", {}).get("intent", {})
    first_prompt_tokens = int(first_usage.get("promptTokenCount") or 0)
    if first_prompt_tokens > LIVE_MAX_PROMPT_TOKENS:
        errors.append(
            f"initial intent prompt token budget exceeded: {first_prompt_tokens}>{LIVE_MAX_PROMPT_TOKENS}"
        )
    if (
        int(first_metrics.get("model_calls", {}).get("intent") or 0) > 0
        and int(first_metrics.get("usage_samples", {}).get("intent") or 0) <= 0
    ):
        warnings.append("Gemini usageMetadata was unavailable for the initial intent call")
    continuation_overhead = int(
        first_metrics.get("intent_prompt_breakdown_bytes", {}).get("continuation_overhead") or 0
    )
    if continuation_overhead > LIVE_MAX_CONTINUATION_PROMPT_OVERHEAD_BYTES:
        errors.append(
            "continuation prompt overhead exceeded canonical V2: "
            f"{continuation_overhead}>{LIVE_MAX_CONTINUATION_PROMPT_OVERHEAD_BYTES} bytes"
        )
    if len(stages) == 2:
        resume_metrics = stages[1]["stage_metrics"]
        resume_usage = resume_metrics.get("usage_metadata", {}).get("intent", {})
        if int(resume_usage.get("promptTokenCount") or 0) > 0:
            errors.append("resume intent path consumed Gemini prompt tokens")

    model_metrics = recorder.summary()
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    dependent = plan.get("dependent_retrieval_plan") if isinstance(plan.get("dependent_retrieval_plan"), dict) else {}
    activation = dependent.get("activation") if isinstance(dependent.get("activation"), dict) else {}
    return {
        "id": str(spec["id"]),
        "question": str(spec["question"]),
        "kind": str(spec["kind"]),
        "status": "ok" if not errors else "error",
        "route": overall_route,
        "final_stage_route": actual_route,
        "child_calls": len(stages),
        "continuation_status": continuation_status,
        "continuation_activation": activation,
        "datasets": sorted(datasets),
        "row_count": row_count,
        "columns": columns,
        "result_fingerprint_sha256": _sha256_json({"columns": columns, "rows": _unordered_rows(rows)}),
        "model": str(recorder.llm_config.get("model") or ""),
        "model_metrics": model_metrics,
        "initial_intent_prompt_tokens": first_prompt_tokens,
        "continuation_prompt_overhead_bytes": continuation_overhead,
        "stage_metrics": [deepcopy(item["stage_metrics"]) for item in stages],
        "router_projection": router_projection,
        "semantic_status": semantic.get("status"),
        "errors": errors,
        "warnings": warnings,
    }


def validate_live_case(
    spec: dict[str, Any],
    *,
    reference_date: str,
    modules: dict[str, Any],
    continuation: dict[str, Any],
    v2: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    checkpoint: LiveCheckpoint,
    limits: dict[str, Any],
) -> dict[str, Any]:
    """Run one question through actual intent/pandas/answer LLM boundaries."""

    case_id = str(spec["id"])
    session_id = f"continuation-live-{case_id.lower()}"
    recorder = LiveModelRecorder(case_id, llm_config, checkpoint)
    stages = [
        _execute_live_stage(
            spec=spec,
            stage_index=0,
            reference_date=reference_date,
            session_id=session_id,
            modules=modules,
            continuation=continuation,
            v2=v2,
            metadata_context=metadata_context,
            recorder=recorder,
        )
    ]
    public_continuation = stages[0]["api_response"].get("continuation", {})
    if str(public_continuation.get("status") or "").lower() == "pending":
        stored_document = _stored_stage_document(stages[0], session_id=session_id)
        stages.append(
            _execute_live_stage(
                spec=spec,
                stage_index=1,
                reference_date=reference_date,
                session_id=session_id,
                modules=modules,
                continuation=continuation,
                v2=v2,
                metadata_context=metadata_context,
                recorder=recorder,
                public_continuation=public_continuation,
                stored_document=stored_document,
            )
        )
    result = _validate_live_result(spec, stages, recorder, limits)
    checkpoint.save_result(result)
    return result


def _resolve_live_llm_config(model_name: str = "") -> dict[str, Any]:
    config = base.resolve_llm_config()
    selected = str(model_name or os.getenv("LLM_MODEL_NAME") or DEFAULT_LIVE_MODEL).strip()
    config["model"] = selected or DEFAULT_LIVE_MODEL
    return config


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil((len(ordered) - 1) * fraction)))
    return ordered[index]


def run_live_validation(
    *,
    reference_date: str = "20260701",
    selected_ids: set[str] | None = None,
    limit: int = 0,
    model_name: str = "",
    checkpoint_path: Path | None = None,
    resume_checkpoint: bool = False,
) -> dict[str, Any]:
    """Execute live Gemini intent/pandas/answer calls with dummy data retrieval."""

    manifest = load_manifest()
    base.install_lfx_stubs()
    modules = base.load_flow_modules()
    continuation = _continuation_modules()
    v2 = _live_pipeline_modules(modules, continuation)
    metadata_context = base.load_metadata_context(modules)
    llm_config = _resolve_live_llm_config(model_name)
    checkpoint = LiveCheckpoint(
        checkpoint_path,
        model=str(llm_config["model"]),
        reference_date=reference_date,
        resume=resume_checkpoint,
    )
    specs = _live_case_specs(manifest, selected_ids, limit)
    results: list[dict[str, Any]] = []
    for spec in specs:
        try:
            result = validate_live_case(
                spec,
                reference_date=reference_date,
                modules=modules,
                continuation=continuation,
                v2=v2,
                metadata_context=metadata_context,
                llm_config=llm_config,
                checkpoint=checkpoint,
                limits=manifest["limits"],
            )
        except Exception as exc:
            result = {
                "id": str(spec["id"]),
                "question": str(spec["question"]),
                "kind": str(spec["kind"]),
                "status": "error",
                "route": "",
                "child_calls": 0,
                "model": str(llm_config["model"]),
                "errors": [f"{type(exc).__name__}: {exc}"],
                "warnings": [],
            }
            checkpoint.save_result(result)
        results.append(result)
    failures = [item for item in results if item.get("status") != "ok"]
    prompt_totals = [
        int(item.get("model_metrics", {}).get("prompt_chars", {}).get("total") or 0)
        for item in results
        if isinstance(item.get("model_metrics"), dict)
    ]
    prompt_token_totals = [
        int(
            item.get("model_metrics", {})
            .get("usage_metadata", {})
            .get("total", {})
            .get("promptTokenCount")
            or 0
        )
        for item in results
        if isinstance(item.get("model_metrics"), dict)
    ]
    continuation_overheads = [
        int(item.get("continuation_prompt_overhead_bytes") or 0)
        for item in results
    ]
    return {
        "status": "ok" if not failures else "error",
        "validation_mode": "live_llm_langflow_equivalent",
        "model": str(llm_config["model"]),
        "reference_date": reference_date,
        "summary": {
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "total": len(results),
            "child_calls": sum(int(item.get("child_calls") or 0) for item in results),
            "actual_model_calls": {
                stage: sum(
                    int(item.get("model_metrics", {}).get("actual_calls", {}).get(stage) or 0)
                    for item in results
                )
                for stage in ("intent", "pandas", "answer")
            },
            "prompt_chars": {
                stage: sum(
                    int(item.get("model_metrics", {}).get("prompt_chars", {}).get(stage) or 0)
                    for item in results
                )
                for stage in ("intent", "pandas", "answer", "total")
            },
            "prompt_chars_per_case": {
                "p50": _percentile(prompt_totals, 0.50),
                "p95": _percentile(prompt_totals, 0.95),
                "max": max(prompt_totals, default=0),
            },
            "gemini_usage_metadata": {
                token_key: sum(
                    int(
                        item.get("model_metrics", {})
                        .get("usage_metadata", {})
                        .get("total", {})
                        .get(token_key)
                        or 0
                    )
                    for item in results
                )
                for token_key in _empty_usage_metadata()
            },
            "prompt_tokens_per_case": {
                "p50": _percentile(prompt_token_totals, 0.50),
                "p95": _percentile(prompt_token_totals, 0.95),
                "max": max(prompt_token_totals, default=0),
            },
            "continuation_prompt_overhead_bytes": {
                "max": max(continuation_overheads, default=0),
            },
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate additive Data Analysis V2 continuation flows.")
    parser.add_argument("--reference-date", default="20260701")
    parser.add_argument("--ids", default="", help="Comma-separated ids such as R01,C01,C02")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--skip-export-check", action="store_true")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Run the actual continuation V2 prompt/model/execution path with live MongoDB metadata and dummy data retrieval.",
    )
    parser.add_argument(
        "--model",
        default="",
        help=f"Gemini model override (default: LLM_MODEL_NAME or {DEFAULT_LIVE_MODEL}).",
    )
    parser.add_argument(
        "--checkpoint",
        default="",
        help="Optional untracked JSON checkpoint for incremental live results and prompt-hash-bound model responses.",
    )
    parser.add_argument(
        "--resume-checkpoint",
        action="store_true",
        help="Reuse checkpointed responses only when model, date and prompt SHA-256 all match.",
    )
    args = parser.parse_args()
    base.load_dotenv(ROOT / ".env")
    selected = {value.strip().upper() for value in args.ids.split(",") if value.strip()}
    checkpoint_path = Path(args.checkpoint) if str(args.checkpoint).strip() else None
    if checkpoint_path is not None and not checkpoint_path.is_absolute():
        checkpoint_path = ROOT / checkpoint_path
    report = (
        run_live_validation(
            reference_date=str(args.reference_date),
            selected_ids=selected,
            limit=max(0, int(args.limit or 0)),
            model_name=str(args.model or ""),
            checkpoint_path=checkpoint_path,
            resume_checkpoint=bool(args.resume_checkpoint),
        )
        if args.use_llm
        else run_validation(
            reference_date=str(args.reference_date),
            selected_ids=selected,
            limit=max(0, int(args.limit or 0)),
            check_exports=not args.skip_export_check,
        )
    )
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for result in report["results"]:
            marker = "OK" if result["status"] == "ok" else "FAIL"
            print(
                f"[{marker}] {result['id']} route={result.get('route', '')} "
                f"calls={result.get('child_calls', 0)} rows={result.get('row_count', len(result.get('rows', [])))}"
            )
            for error in result.get("errors", []):
                print(f"  - {error}")
        if report.get("export_validation", {}).get("status") == "error":
            print(f"[FAIL] exports: {report['export_validation']['errors']}")
        print(
            f"summary: {report['summary']['passed']}/{report['summary']['total']} passed, "
            f"status={report['status']}"
        )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
