from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .replace_process_group_domains import (
        DEFAULT_COLLECTION,
        DEFAULT_DATABASE,
        DOMAIN_FLOW_DIR,
        DOMAIN_KNOWLEDGE_PATH,
        ROOT,
        load_dotenv,
        parse_process_group_blocks,
    )
except ImportError:
    from replace_process_group_domains import (
        DEFAULT_COLLECTION,
        DEFAULT_DATABASE,
        DOMAIN_FLOW_DIR,
        DOMAIN_KNOWLEDGE_PATH,
        ROOT,
        load_dotenv,
        parse_process_group_blocks,
    )


PROMPT_PATH = DOMAIN_FLOW_DIR / "03_saving_prompt_template_ko.md"
SYSTEM_MESSAGE = "Return only the JSON object requested by the prompt. Do not add markdown or prose."

# 현재 운영 기준에서 제거할 명확한 중복·구버전 문서입니다.
# 신규/교체 문서는 이 목록처럼 직접 작성하지 않고 Domain Saving LLM 출력으로만 생성합니다.
OBSOLETE_IDS = [
    "domain:analysis_recipes:PRODUCT_RANKING_ANALYSIS",
    "domain:analysis_recipes:filter_by_oper_sequence_range",
    "domain:metric_terms:WIP_QUANTITY_SUM",
    "domain:pandas_function_cases:apply_shift_performance_filter_rule",
    "domain:pandas_function_cases:calculate_equipment_count",
    "domain:pandas_function_cases:calculate_input_production",
    "domain:pandas_function_cases:calculate_production_by_oper_name",
    "domain:pandas_function_cases:calculate_production_total",
    "domain:pandas_function_cases:calculate_wafer_out_quantity",
    "domain:pandas_function_cases:calculate_wip_boh",
    "domain:pandas_function_cases:calculate_wip_eoh",
    "domain:pandas_function_cases:calculate_wip_total",
    "domain:pandas_function_cases:component_token_product_lookup",
    "domain:pandas_function_cases:interpret_product_unit_analysis_request",
    "domain:pandas_function_cases:join_and_group_by_product",
    "domain:pandas_function_cases:join_datasets_by_product_keys",
    "domain:pandas_function_cases:raw_data_display_rule",
    "domain:process_groups:BG_PROCESS_GROUP",
    "domain:product_key_columns:DEFAULT_PRODUCT_KEYS",
    "domain:product_terms:DEVICE_AGGREGATION_CONSTRAINT",
    "domain:product_terms:PRODUCT_TOKEN_MATCH_USAGE_RULE",
]

EXPECTED_FINAL_KEYS = {
    "analysis_recipes:equipment_assignment_uph_join",
    "analysis_recipes:product_grain_and_join_policy",
    "analysis_recipes:raw_data_display_policy",
    "analysis_recipes:recipe_id_starts_with",
    "analysis_recipes:current_hold_lot_selection",
    "analysis_recipes:uph_result_policy",
    "pandas_function_cases:ordered_process_range",
    "pandas_function_cases:product_token_match",
    "pandas_function_cases:sample_passthrough_demo",
    "product_key_columns:standard_product_keys",
    "quantity_terms:wip_boh_quantity",
    "quantity_terms:pkg_out_quantity",
    "quantity_terms:target_data",
}


def _extract_block(text: str, start: str, end: str | None) -> str:
    """시작·종료 표식을 이용해 Domain Saving Flow에 넣을 원문 블록을 꺼냅니다."""

    start_index = text.index(start)
    end_index = text.index(end, start_index) if end else len(text)
    return text[start_index:end_index].strip()


def build_registration_requests(text: str) -> list[dict[str, Any]]:
    """현재 domain_knowledge.txt에서 운영 반영 대상 원문과 예상 key를 구성합니다."""

    requests: list[dict[str, Any]] = []
    for item in parse_process_group_blocks(text):
        requests.append(
            {
                "name": f"process_group_{item['key']}",
                "raw_text": item["_raw_text"],
                "expected_keys": [f"process_groups:{item['key']}"],
                "expected_payload": deepcopy(item["payload"]),
            }
        )

    specs = [
        (
            "target_data",
            "생산 계획 metric 계약을 등록해줘.",
            "장비 대수, 설비 대수, 장비 수, 설비 수, 몇 대는",
            ["quantity_terms:target_data"],
        ),
        (
            "pkg_out_quantity",
            "PKG OUT 실적 metric을 등록해줘.",
            "BOH 재공과 아침 재공 기준일 규칙을 등록해줘.",
            ["quantity_terms:pkg_out_quantity"],
        ),
        (
            "wip_boh_quantity",
            "BOH 재공과 아침 재공 기준일 규칙을 등록해줘.",
            "계산 로직을 등록해줘.",
            ["quantity_terms:wip_boh_quantity"],
        ),
        (
            "standard_product_keys",
            "표준 제품 키를 등록해줘.",
            "제품 집계·결합 정책을 등록해줘.",
            ["product_key_columns:standard_product_keys"],
        ),
        (
            "product_grain_and_join_policy",
            "제품 집계·결합 정책을 등록해줘.",
            "장비 배정과 Recipe UPH 결합 규칙을 등록해줘.",
            ["analysis_recipes:product_grain_and_join_policy"],
        ),
        (
            "equipment_assignment_uph_join",
            "장비 배정과 Recipe UPH 결합 규칙을 등록해줘.",
            "UPH 결과 해석 규칙을 등록해줘.",
            ["analysis_recipes:equipment_assignment_uph_join"],
        ),
        (
            "uph_result_policy",
            "UPH 결과 해석 규칙을 등록해줘.",
            "원본 상세 데이터 표시 규칙을 등록해줘.",
            ["analysis_recipes:uph_result_policy"],
        ),
        (
            "raw_data_display_policy",
            "원본 상세 데이터 표시 규칙을 등록해줘.",
            "RECIPE 번호 조회 규칙을 등록해줘.",
            ["analysis_recipes:raw_data_display_policy"],
        ),
        (
            "recipe_id_starts_with",
            "RECIPE 번호 조회 규칙을 등록해줘.",
            "HOLD LOT와 HOLD 이력 조회 규칙을 등록해줘.",
            ["analysis_recipes:recipe_id_starts_with"],
        ),
        (
            "current_hold_lot_selection",
            "HOLD LOT와 HOLD 이력 조회 규칙을 등록해줘.",
            "pandas function case 등록 규칙을 등록해줘.",
            ["analysis_recipes:current_hold_lot_selection"],
        ),
        (
            "ordered_process_range",
            "공정 순서 구간 필터 function case를 등록해줘.",
            "제품 속성 token 매칭 function case를 등록해줘.",
            ["pandas_function_cases:ordered_process_range"],
        ),
        (
            "product_token_match",
            "제품 속성 token 매칭 function case를 등록해줘.",
            "다중 helper 형식 확인용 더미 function case를 등록해줘.",
            ["pandas_function_cases:product_token_match"],
        ),
        (
            "sample_passthrough_demo",
            "다중 helper 형식 확인용 더미 function case를 등록해줘.",
            None,
            ["pandas_function_cases:sample_passthrough_demo"],
        ),
    ]
    for name, start, end, expected_keys in specs:
        requests.append(
            {
                "name": name,
                "raw_text": _extract_block(text, start, end),
                "expected_keys": expected_keys,
            }
        )
    return requests


def resolve_llm_config() -> dict[str, Any]:
    """Flow JSON의 Google 모델 설정과 같은 기본값으로 실행 설정을 확정합니다."""

    api_key = (
        os.getenv("GOOGLE_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY, GEMINI_API_KEY 또는 LLM_API_KEY가 필요합니다.")
    return {
        "api_key": api_key,
        "model": (
            os.getenv("DOMAIN_SAVING_LLM_MODEL", "").strip()
            or "gemini-3.5-flash-lite"
        ).removeprefix("models/"),
        "temperature": 0.1,
        "max_output_tokens": 8192,
        "timeout": int(float(os.getenv("LLM_TIMEOUT_SECONDS", "120") or 120)),
    }


def call_google_model(prompt: str, config: dict[str, Any]) -> str:
    """Langflow Language Model 노드와 같은 Google JSON 생성 호출을 수행합니다."""

    model = urllib.parse.quote(str(config["model"]), safe="")
    api_key = urllib.parse.quote(str(config["api_key"]), safe="")
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_MESSAGE}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": config["temperature"],
            "maxOutputTokens": config["max_output_tokens"],
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
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Google model HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google model request failed: {exc.reason}") from exc

    parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    )
    if not text.strip():
        raise RuntimeError("Google model response did not contain text.")
    return text


def _logical_keys(items: list[Any]) -> list[str]:
    return sorted(
        f"{item.get('section')}:{item.get('key')}"
        for item in items
        if isinstance(item, dict)
    )


def _semantic_errors(
    request_spec: dict[str, Any],
    items: list[Any],
) -> list[dict[str, Any]]:
    """LLM 후보가 원문의 핵심 의미를 누락·추정하지 않았는지 추가 확인합니다."""

    errors: list[dict[str, Any]] = []
    item_by_key = {
        f"{item.get('section')}:{item.get('key')}": item
        for item in items
        if isinstance(item, dict)
    }
    name = str(request_spec.get("name") or "")
    if name.startswith("process_group_"):
        key = request_spec["expected_keys"][0]
        payload = (item_by_key.get(key) or {}).get("payload")
        if payload != request_spec.get("expected_payload"):
            errors.append(
                {
                    "type": "process_group_payload_mismatch",
                    "message": f"{key}의 display_name, aliases, field 또는 processes가 원문과 다릅니다.",
                }
            )
    elif name == "target_data":
        payload = (
            item_by_key.get("quantity_terms:target_data") or {}
        ).get("payload", {})
        if payload.get("data_source") != "target":
            errors.append({
                "type": "target_data_source_mismatch",
                "message": "계획 metric의 data_source는 target이어야 합니다.",
            })
        if payload.get("metric_columns") != ["INPUT_PLAN_QTY", "OUT_PLAN_QTY"]:
            errors.append({
                "type": "target_metric_columns_mismatch",
                "message": "계획 metric은 canonical INPUT_PLAN_QTY와 OUT_PLAN_QTY를 순서대로 사용해야 합니다.",
            })
        payload_text = json.dumps(payload, ensure_ascii=False)
        if "PLAN_QTY" in payload_text.replace("INPUT_PLAN_QTY", "").replace("OUT_PLAN_QTY", ""):
            errors.append({
                "type": "invented_generic_plan_metric",
                "message": "등록되지 않은 포괄 PLAN_QTY metric을 만들면 안 됩니다.",
            })
    elif name == "wip_boh_quantity":
        payload = (
            item_by_key.get("quantity_terms:wip_boh_quantity") or {}
        ).get("payload", {})
        expected_temporal = {
            "business_timepoint": "BOH",
            "dataset_family": "wip_history",
            "dataset_key": "wip",
            "date_param": "DATE",
            "requested_date_offset_days": -1,
            "disallowed_dataset_keys": ["wip_today"],
            "inherit_filters": True,
        }
        expected_fields = {
            "data_source": "wip",
            "column": "WIP",
            "aggregation_method": "sum",
            "temporal_semantics": expected_temporal,
        }
        for field, expected_value in expected_fields.items():
            if payload.get(field) != expected_value:
                errors.append(
                    {
                        "type": "wip_boh_temporal_contract_mismatch",
                        "message": f"BOH 재공 payload.{field}가 등록 원문과 다릅니다.",
                    }
                )
    elif name == "pkg_out_quantity":
        payload = (
            item_by_key.get("quantity_terms:pkg_out_quantity") or {}
        ).get("payload", {})
        expected_fields = {
            "data_source": "production",
            "column": "PRODUCTION",
            "aggregation_method": "sum",
        }
        for field, expected_value in expected_fields.items():
            if payload.get(field) != expected_value:
                errors.append(
                    {
                        "type": "pkg_out_metric_contract_mismatch",
                        "message": f"PKG OUT payload.{field}가 등록 원문과 다릅니다.",
                    }
                )
        conditions = payload.get("conditions")
        conditions = conditions if isinstance(conditions, list) else [conditions]
        expected_condition = {
            "column": "OPER_NAME",
            "operator": "eq",
            "value": "PKG OUT",
        }
        if expected_condition not in conditions:
            errors.append(
                {
                    "type": "pkg_out_process_condition_missing",
                    "message": "PKG OUT metric의 OPER_NAME 실행 조건이 누락되었습니다.",
                }
            )
    elif name == "standard_product_keys":
        payload = (
            item_by_key.get("product_key_columns:standard_product_keys") or {}
        ).get("payload", {})
        expected = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
        if payload.get("columns") != expected:
            errors.append(
                {
                    "type": "standard_product_keys_mismatch",
                    "message": "표준 제품 키가 현재 기준과 다릅니다.",
                }
            )
        if "제품별" not in payload.get("aliases", []):
            errors.append(
                {
                    "type": "missing_product_key_alias",
                    "message": "표준 제품 키에 제품별 선택 단서가 누락되었습니다.",
                }
            )
    elif name == "product_grain_and_join_policy":
        payload = (
            item_by_key.get("analysis_recipes:product_grain_and_join_policy") or {}
        ).get("payload", {})
        payload_text = json.dumps(payload, ensure_ascii=False)
        if "join_type" in payload:
            errors.append(
                {
                    "type": "inferred_join_type",
                    "message": "원문에 없는 join_type을 제품 정책에 고정했습니다.",
                }
            )
        for token in ("DEVICE_DESC", "product_token_match", "상위 N개 제품"):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "missing_product_policy",
                        "message": f"제품 정책 payload에 {token} 기준이 누락되었습니다.",
                    }
                )
        if "제품별" not in payload.get("aliases", []):
            errors.append(
                {
                    "type": "missing_product_policy_alias",
                    "message": "제품 집계 정책에 제품별 선택 단서가 누락되었습니다.",
                }
            )
    elif name == "equipment_assignment_uph_join":
        payload = (
            item_by_key.get("analysis_recipes:equipment_assignment_uph_join") or {}
        ).get("payload", {})
        expected = {
            "source_datasets": ["equipment_assign", "eqp_uph"],
            "join_type": "left",
            "join_keys": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "preserve_left_rows": True,
            "selection_criteria": {
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
            },
        }
        for field, expected_value in expected.items():
            if payload.get(field) != expected_value:
                errors.append(
                    {
                        "type": "equipment_join_policy_mismatch",
                        "message": f"장비-UPH 결합 정책의 {field} 값이 원문과 다릅니다.",
                    }
                )
        for field in ("left_key_mappings", "right_key_mappings"):
            if field in payload:
                errors.append(
                    {
                        "type": "physical_join_mapping_in_domain",
                        "message": f"장비-UPH Domain에는 Table Catalog 소유 물리 매핑 {field}를 저장하지 않습니다.",
                    }
                )
        aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
        if "장비별 UPH" in aliases:
            errors.append(
                {
                    "type": "equipment_join_alias_too_broad",
                    "message": "배정 장비를 요구하지 않는 일반 장비별 UPH를 결합 recipe alias로 사용하면 안 됩니다.",
                }
            )
        payload_text = json.dumps(payload, ensure_ascii=False)
        for token in ("목록", "대수", "eqp_uph"):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "equipment_join_selection_scope_missing",
                        "message": f"장비-UPH 결합 정책에 선택 범위 단서 {token}가 누락되었습니다.",
                    }
                )
    elif name == "uph_result_policy":
        payload = (
            item_by_key.get("analysis_recipes:uph_result_policy") or {}
        ).get("payload", {})
        payload_text = json.dumps(payload, ensure_ascii=False)
        for token in ("UPH", "평균", "EQP_MODEL", "RECIPE_ID"):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "uph_result_policy_mismatch",
                        "message": f"UPH 결과 정책에 {token} 기준이 누락되었습니다.",
                    }
                )
        if "sum" in payload_text.lower() or "합산" not in payload_text:
            errors.append(
                {
                    "type": "uph_result_policy_additive_mismatch",
                    "message": "UPH 비가산·합산 금지 기준이 정확히 보존되지 않았습니다.",
                }
            )
    elif name == "raw_data_display_policy":
        payload = (
            item_by_key.get("analysis_recipes:raw_data_display_policy") or {}
        ).get("payload", {})
        if payload.get("aliases") != ["원본", "RAW DATA", "세부 데이터", "전체 데이터"]:
            errors.append(
                {
                    "type": "raw_data_alias_mismatch",
                    "message": "원본 상세 데이터 선택 표현이 원문과 다릅니다.",
                }
            )
    elif name == "recipe_id_starts_with":
        payload = (
            item_by_key.get("analysis_recipes:recipe_id_starts_with") or {}
        ).get("payload", {})
        payload_text = json.dumps(payload, ensure_ascii=False)
        for token in ("RECIPE_ID", "starts_with", "R1234", "eq", "contains"):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "recipe_prefix_policy_mismatch",
                        "message": f"RECIPE prefix 정책에 {token} 기준이 누락되었습니다.",
                    }
                )
    elif name == "current_hold_lot_selection":
        payload = (
            item_by_key.get("analysis_recipes:current_hold_lot_selection") or {}
        ).get("payload", {})
        payload_text = json.dumps(payload, ensure_ascii=False)
        for token in (
            "lot_status",
            "hold_history",
            "HOLD_STAT",
            "OnHold",
            "HOLD_CD",
            "HOLD_TM",
            "HOLD_DESC",
            "LOT_ID",
            "required_params",
            "빈 문자열",
            "선행 결과",
            "previous_result",
            "내림차순",
            "가장 최근",
            "result_ref",
        ):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "hold_dataset_selection_policy_mismatch",
                        "message": f"HOLD dataset 선택 정책에 {token} 기준이 누락되었습니다.",
                    }
                )
    elif name == "ordered_process_range":
        payload = (
            item_by_key.get("pandas_function_cases:ordered_process_range") or {}
        ).get("payload", {})
        if payload.get("function_name") != "filter_ordered_range":
            errors.append(
                {
                    "type": "function_name_mismatch",
                    "message": "ordered_process_range function_name이 잘못되었습니다.",
                }
            )
        if payload.get("execution_contract") != {
            "source_filter_order": "after_helper"
        }:
            errors.append(
                {
                    "type": "execution_contract_mismatch",
                    "message": (
                        "ordered_process_range의 source filter는 "
                        "helper 실행 뒤에 적용되어야 합니다."
                    ),
                }
            )
    elif name == "product_token_match":
        payload = (
            item_by_key.get("pandas_function_cases:product_token_match") or {}
        ).get("payload", {})
        payload_text = json.dumps(payload, ensure_ascii=False)
        if payload.get("function_name") != "match_product_tokens":
            errors.append(
                {
                    "type": "function_name_mismatch",
                    "message": "product_token_match function_name이 잘못되었습니다.",
                }
            )
        for token in ("ORG", "FCBGA", "LEAD", "MCP_NO"):
            if token not in payload_text:
                errors.append(
                    {
                        "type": "missing_product_token_rule",
                        "message": f"product_token_match에 {token} 규칙이 누락되었습니다.",
                    }
                )
    elif name == "sample_passthrough_demo":
        payload = (
            item_by_key.get("pandas_function_cases:sample_passthrough_demo") or {}
        ).get("payload", {})
        if payload.get("function_name") != "sample_passthrough_helper":
            errors.append(
                {
                    "type": "function_name_mismatch",
                    "message": "sample_passthrough_demo function_name이 잘못되었습니다.",
                }
            )
    return errors


def _load_components() -> dict[str, Any]:
    """실제 Domain Saving 컴포넌트의 일반 Python 함수를 로드합니다."""

    sys.path.insert(0, str(ROOT))
    from tools import validate_representative_questions as harness

    harness.install_lfx_stubs()
    return {
        "request": harness.load_module(
            DOMAIN_FLOW_DIR / "00_domain_saving_request_loader.py"
        ),
        "variables": harness.load_module(
            DOMAIN_FLOW_DIR / "03_domain_saving_variables_builder.py"
        ),
        "normalizer": harness.load_module(
            DOMAIN_FLOW_DIR / "04_domain_saving_result_normalizer.py"
        ),
        "similarity": harness.load_module(
            DOMAIN_FLOW_DIR / "05_domain_similarity_checker.py"
        ),
        "writer": harness.load_module(
            DOMAIN_FLOW_DIR / "07_domain_review_writer.py"
        ),
    }


def generate_and_validate(
    registration_requests: list[dict[str, Any]],
    *,
    components: dict[str, Any],
    llm_config: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
    cached_responses: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """LLM 생성부터 dry-run writer까지 실제 Flow 순서로 실행합니다."""

    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for request_spec in registration_requests:
        payload = components["request"].build_request(
            request_spec["raw_text"],
            duplicate_action="replace",
            dry_run=True,
        )
        variables = components["variables"].build_variables(payload)
        prompt = prompt_template.format(**variables)
        llm_response = str((cached_responses or {}).get(request_spec["name"]) or "")
        response_source = "cached_google_response" if llm_response else "google_api"
        if not llm_response:
            llm_response = call_google_model(prompt, llm_config)
        payload = components["normalizer"].normalize_authoring(
            payload,
            llm_response,
        )
        generated_keys = _logical_keys(payload.get("items", []))
        expected_keys = sorted(request_spec["expected_keys"])
        key_match = generated_keys == expected_keys
        semantic_errors = _semantic_errors(
            request_spec,
            payload.get("items", []),
        )

        if key_match and not semantic_errors:
            payload = components["similarity"].check_similarity(
                payload,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            payload = components["writer"].review_and_write(
                payload,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
        write_result = (
            payload.get("write_result")
            if isinstance(payload.get("write_result"), dict)
            else {}
        )
        success = (
            key_match
            and not semantic_errors
            and bool(write_result.get("success"))
            and not payload.get("errors")
        )
        results.append(
            {
                "name": request_spec["name"],
                "expected_keys": expected_keys,
                "generated_keys": generated_keys,
                "key_match": key_match,
                "semantic_errors": semantic_errors,
                "success": success,
                "response_source": response_source,
                "dry_run_result": deepcopy(write_result),
                "normalization_errors": deepcopy(payload.get("errors") or []),
                "llm_response": llm_response,
            }
        )
        if success:
            prepared.append(
                {
                    "name": request_spec["name"],
                    "raw_text": request_spec["raw_text"],
                    "expected_keys": expected_keys,
                    "items": deepcopy(payload.get("items") or []),
                    "llm_response": llm_response,
                }
            )
    return prepared, results


def apply_reconciliation(
    prepared: list[dict[str, Any]],
    *,
    components: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> dict[str, Any]:
    """폐기 문서를 지운 뒤 LLM 생성 후보를 실제 Flow matcher/writer로 저장합니다."""

    from pymongo import MongoClient

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    collection = client[mongo_database][collection_name]
    backup = list(collection.find({}))
    deletion_result: dict[str, Any] = {}
    writes: list[dict[str, Any]] = []
    try:
        found_obsolete = sorted(
            str(item["_id"])
            for item in collection.find({"_id": {"$in": OBSOLETE_IDS}}, {"_id": 1})
        )
        deleted = collection.delete_many({"_id": {"$in": OBSOLETE_IDS}})
        deletion_result = {
            "requested_ids": OBSOLETE_IDS,
            "found_ids": found_obsolete,
            "deleted_count": int(deleted.deleted_count),
        }

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
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            write_result = (
                payload.get("write_result")
                if isinstance(payload.get("write_result"), dict)
                else {}
            )
            writes.append(
                {
                    "name": prepared_item["name"],
                    "generated_keys": _logical_keys(payload.get("items") or []),
                    "success": bool(write_result.get("success")),
                    "write_result": deepcopy(write_result),
                    "errors": deepcopy(payload.get("errors") or []),
                }
            )
            if not write_result.get("success") or payload.get("errors"):
                raise RuntimeError(
                    f"Domain Saving live write failed: {prepared_item['name']}"
                )

        remaining_obsolete = sorted(
            str(item["_id"])
            for item in collection.find({"_id": {"$in": OBSOLETE_IDS}}, {"_id": 1})
        )
        final_keys = {
            f"{item.get('section')}:{item.get('key')}"
            for item in collection.find({}, {"section": 1, "key": 1})
        }
        missing_expected = sorted(EXPECTED_FINAL_KEYS - final_keys)
        if remaining_obsolete or missing_expected:
            raise RuntimeError(
                "post-apply verification failed: "
                f"remaining_obsolete={remaining_obsolete}, "
                f"missing_expected={missing_expected}"
            )
        return {
            "success": True,
            "deletion": deletion_result,
            "writes": writes,
            "final_count": collection.count_documents({}),
            "remaining_obsolete_ids": [],
            "missing_expected_keys": [],
        }
    except Exception:
        collection.delete_many({})
        if backup:
            collection.insert_many(backup)
        raise
    finally:
        client.close()


def apply_selected_reconciliation(
    prepared: list[dict[str, Any]],
    *,
    components: dict[str, Any],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> dict[str, Any]:
    """선택한 canonical Domain 문서만 replace하고 실패 시 그 문서들만 복원합니다."""

    from pymongo import MongoClient

    target_ids = [
        f"domain:{item.get('section')}:{item.get('key')}"
        for prepared_item in prepared
        for item in prepared_item.get("items", [])
        if isinstance(item, dict)
    ]
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    collection = client[mongo_database][collection_name]
    backups = {
        doc_id: collection.find_one({"_id": doc_id})
        for doc_id in target_ids
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
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            write_result = (
                payload.get("write_result")
                if isinstance(payload.get("write_result"), dict)
                else {}
            )
            writes.append(
                {
                    "name": prepared_item["name"],
                    "generated_keys": _logical_keys(payload.get("items") or []),
                    "success": bool(write_result.get("success")),
                    "write_result": deepcopy(write_result),
                    "errors": deepcopy(payload.get("errors") or []),
                }
            )
            if not write_result.get("success") or payload.get("errors"):
                raise RuntimeError(
                    f"Domain Saving selected live write failed: {prepared_item['name']}"
                )

        saved_ids = sorted(
            str(item["_id"])
            for item in collection.find(
                {"_id": {"$in": target_ids}},
                {"_id": 1},
            )
        )
        missing_ids = sorted(set(target_ids) - set(saved_ids))
        if missing_ids:
            raise RuntimeError(
                f"selected post-apply verification failed: missing_ids={missing_ids}"
            )
        return {
            "success": True,
            "mode": "selected_replace",
            "writes": writes,
            "verified_ids": saved_ids,
        }
    except Exception:
        for doc_id in target_ids:
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
            "Google model과 실제 Domain Saving 컴포넌트 순서를 사용해 "
            "agent_v4_domain_items를 현재 기준으로 정리합니다."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Dry-run 검증이 모두 성공한 뒤 MongoDB 삭제·저장을 실제 적용합니다.",
    )
    parser.add_argument(
        "--output",
        default="validation_outputs/domain_saving_reconciliation_20260725.json",
        help="비밀값을 제외한 전체 검증·저장 결과 파일입니다.",
    )
    parser.add_argument(
        "--resume-from",
        default="",
        help="이전 드라이런 결과에서 성공한 Google 응답을 재사용합니다.",
    )
    parser.add_argument(
        "--refresh",
        nargs="*",
        default=[],
        help="resume 결과가 있어도 지정한 요청 이름은 Google API로 다시 생성합니다.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=[],
        help="지정한 request name만 검증·replace합니다. 이 모드에서는 obsolete 문서를 삭제하지 않습니다.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI가 필요합니다.")
    mongo_database = (
        os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    )
    collection_name = (
        os.getenv("MONGODB_DOMAIN_COLLECTION", DEFAULT_COLLECTION).strip()
        or DEFAULT_COLLECTION
    )
    llm_config = resolve_llm_config()
    components = _load_components()
    requests = build_registration_requests(
        DOMAIN_KNOWLEDGE_PATH.read_text(encoding="utf-8")
    )
    if args.only:
        selected_names = {str(name).strip() for name in args.only if str(name).strip()}
        requests = [
            item for item in requests
            if str(item.get("name") or "") in selected_names
        ]
        found_names = {str(item.get("name") or "") for item in requests}
        missing_names = sorted(selected_names - found_names)
        if missing_names:
            raise RuntimeError(
                "알 수 없는 Domain Saving request name입니다: "
                + ", ".join(missing_names)
            )
    cached_responses: dict[str, str] = {}
    if args.resume_from:
        resume_path = Path(args.resume_from)
        if not resume_path.is_absolute():
            resume_path = ROOT / resume_path
        resume_report = json.loads(resume_path.read_text(encoding="utf-8"))
        for item in resume_report.get("dry_run_results", []):
            if (
                isinstance(item, dict)
                and item.get("success")
                and item.get("name")
                and item.get("llm_response")
            ):
                cached_responses[str(item["name"])] = str(item["llm_response"])
    for name in args.refresh:
        cached_responses.pop(str(name), None)
    prepared, dry_run_results = generate_and_validate(
        requests,
        components=components,
        llm_config=llm_config,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=collection_name,
        cached_responses=cached_responses,
    )
    dry_run_success = (
        len(prepared) == len(requests)
        and all(item["success"] for item in dry_run_results)
    )

    apply_result: dict[str, Any] = {}
    status = "dry_run_ok" if dry_run_success else "dry_run_error"
    if args.apply:
        if not dry_run_success:
            raise RuntimeError("Dry-run 검증이 모두 성공하지 않아 실제 저장을 중단했습니다.")
        apply_result = (
            apply_selected_reconciliation(
                prepared,
                components=components,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
            if args.only
            else apply_reconciliation(
                prepared,
                components=components,
                mongo_uri=mongo_uri,
                mongo_database=mongo_database,
                collection_name=collection_name,
            )
        )
        status = "applied" if apply_result.get("success") else "apply_error"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "database": mongo_database,
        "collection_name": collection_name,
        "llm": {
            "provider": "Google Generative AI",
            "model": llm_config["model"],
            "temperature": llm_config["temperature"],
            "max_output_tokens": llm_config["max_output_tokens"],
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
    print(
        json.dumps(
            {
                "status": status,
                "output": str(output_path),
                "request_count": len(requests),
                "prepared_count": len(prepared),
                "apply_result": apply_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status in {"dry_run_ok", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
