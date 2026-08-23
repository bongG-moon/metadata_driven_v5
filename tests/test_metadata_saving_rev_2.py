from __future__ import annotations

import hashlib
import json
from pathlib import Path

from langchain_core.prompts import PromptTemplate
from lfx.base.prompts.api_utils import validate_prompt

from component_test_support import ROOT, load_module
from tools.build_metadata_saving_rev_2_flows import REV2_DISPLAY_NAMES, write_rev_2_flows
from tools.validate_flow_component_sources import audit_rev2_repository


REV2_ROOT = ROOT / "langflow_components" / "metadata_saving_rev_2_common"
ORIGINAL_FLOW_PATHS = [
    ROOT / "flow_exports" / "domain_saving_flow_v5_standalone.json",
    ROOT / "flow_exports" / "table_catalog_saving_flow_v5_standalone.json",
    ROOT / "flow_exports" / "main_flow_filter_saving_flow_v5_standalone.json",
    ROOT / "import_ready_flows" / "02_domain_saving_flow_v5_standalone.json",
    ROOT / "import_ready_flows" / "03_table_catalog_saving_flow_v5_standalone.json",
    ROOT / "import_ready_flows" / "04_main_flow_filter_saving_flow_v5_standalone.json",
]


def _modules():
    return (
        load_module(REV2_ROOT / "02_metadata_authoring_context_builder_rev_2.py"),
        load_module(REV2_ROOT / "04_metadata_authoring_refinement_normalizer_rev_2.py"),
        load_module(REV2_ROOT / "05_metadata_authoring_extraction_variables_rev_2.py"),
        load_module(REV2_ROOT / "05b_metadata_authoring_candidate_repair_rev_2.py"),
        load_module(REV2_ROOT / "06_metadata_authoring_contract_guard_rev_2.py"),
        load_module(REV2_ROOT / "08_metadata_authoring_response_enricher_rev_2.py"),
        load_module(REV2_ROOT / "09_metadata_authoring_message_adapter_rev_2.py"),
    )


def _table(dataset_key: str, display_name: str, aliases: list[str], mapping: dict[str, list[str]]) -> dict:
    return {
        "dataset_key": dataset_key,
        "status": "active",
        "payload": {
            "display_name": display_name,
            "aliases": aliases,
            "filter_mappings": mapping,
            "standard_column_aliases": {
                "EQP_MODEL": ["장비모델", "장비 모델", "EQUIP_MODEL"],
                "RECIPE_ID": ["Recipe", "레시피"],
                "OPER_NAME": ["공정", "공정명"],
            },
            "columns": sorted({physical for values in mapping.values() for physical in values}),
        },
    }


def _tables() -> list[dict]:
    return [
        _table(
            "equipment_assign",
            "Equipment Assign 현황",
            ["장비 Assign 현황", "장비 배정 테이블"],
            {
                "EQP_ID": ["EQUIP_ID"],
                "EQP_MODEL": ["EQUIP_MODEL"],
                "RECIPE_ID": ["RECIPE_ID"],
                "OPER_NAME": ["OPER_NM"],
            },
        ),
        _table(
            "eqp_uph",
            "Equipment UPH",
            ["장비 UPH 테이블", "장비 UPH 데이터"],
            {
                "EQP_MODEL": ["EQUIP_MODEL"],
                "RECIPE_ID": ["RECIPE_ID"],
                "OPER_NAME": ["OPER_NAME"],
                "UPH": ["UPH"],
            },
        ),
    ]


def _filters() -> list[dict]:
    return [
        {"filter_key": "EQP_MODEL", "status": "active", "payload": {"display_name": "장비 모델", "aliases": ["장비모델", "장비 모델", "설비 모델"], "column_candidates": ["EQP_MODEL", "EQUIP_MODEL"]}},
        {"filter_key": "RECIPE_ID", "status": "active", "payload": {"display_name": "Recipe ID", "aliases": ["Recipe", "레시피"], "column_candidates": ["RECIPE_ID"]}},
        {"filter_key": "OPER_NAME", "status": "active", "payload": {"display_name": "공정명", "aliases": ["공정", "공정명"], "column_candidates": ["OPER_NAME", "OPER_NM"]}},
    ]


def _domains() -> list[dict]:
    return [
        {
            "section": "quantity_terms",
            "key": "equipment_count",
            "status": "active",
            "payload": {"display_name": "장비보유댓수", "aliases": ["장비 대수", "설비 대수", "장비 수", "몇 대"]},
        },
        {
            "section": "analysis_recipes",
            "key": "uph_result_policy",
            "status": "active",
            "payload": {"display_name": "UPH 결과 정책", "aliases": ["UPH"]},
        },
    ]


def _snapshot(key: str, items: list[dict]) -> dict:
    return {key: items, "metadata_load": {"status": "ok", "count": len(items), "database": "datagov", "collection_name": key, "errors": []}}


def _context(
    raw_text: str,
    tables: list[dict] | None = None,
    domains: list[dict] | None = None,
    filters: list[dict] | None = None,
) -> dict:
    context_builder, *_ = _modules()
    result = context_builder.build_authoring_context(
        {
            "metadata_type": "domain",
            "request": {"raw_text": raw_text, "duplicate_action": "skip", "dry_run": True},
            "refinement": {},
            "errors": [],
            "warnings": [],
            "trace": {},
        },
        _snapshot("domain_items", domains if domains is not None else []),
        _snapshot("table_catalog_items", tables or _tables()),
        _snapshot("main_flow_filters", filters or _filters()),
    )
    return result["payload"]


def _table_context(raw_text: str) -> dict:
    context_builder, *_ = _modules()
    result = context_builder.build_authoring_context(
        {
            "metadata_type": "table_catalog",
            "request": {"raw_text": raw_text, "duplicate_action": "skip", "dry_run": True},
            "refinement": {},
            "errors": [],
            "warnings": [],
            "trace": {},
        },
        _snapshot("domain_items", _domains()),
        _snapshot("table_catalog_items", _tables()),
        _snapshot("main_flow_filters", _filters()),
    )
    return result["payload"]


def _filter_context(raw_text: str) -> dict:
    context_builder, *_ = _modules()
    result = context_builder.build_authoring_context(
        {
            "metadata_type": "main_flow_filter",
            "request": {"raw_text": raw_text, "duplicate_action": "skip", "dry_run": True},
            "refinement": {},
            "errors": [],
            "warnings": [],
            "trace": {},
        },
        _snapshot("domain_items", _domains()),
        _snapshot("table_catalog_items", _tables()),
        _snapshot("main_flow_filters", _filters()),
    )
    return result["payload"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_refinement_prompt_has_only_expected_dynamic_variables() -> None:
    template = (REV2_ROOT / "03_metadata_authoring_refinement_prompt_ko.md").read_text(encoding="utf-8")
    variables = validate_prompt(template)
    assert variables == ["metadata_type", "metadata_context", "source_text"]
    rendered = PromptTemplate(template=template, input_variables=variables).format(
        metadata_type="domain",
        metadata_context="{}",
        source_text="장비 UPH 규칙",
    )
    assert '"resolved_references"' in rendered
    assert '"needs_more_input"' in rendered
    assert "특정 표기 형식을 강제하지 않는다" in template
    assert "사용자 원문을 단순 반복하지 말고" in template
    assert "한 줄로 길게 이어 쓰지 않는다" in template
    assert "SQL 또는 조회 쿼리가 있으면" in template
    assert "Markdown 코드 펜스로 감싸지 않으며" in template


def test_one_line_main_filter_refinement_is_grouped_into_copy_ready_paragraphs() -> None:
    _, refinement_normalizer, *_ = _modules()
    raw = """장비모델 필터를 Main Flow Filter 메타데이터로 등록해줘.

filter_key는 장비모델이고 status는 active야.
표시명은 장비 모델이야.
장비모델, 장비 모델, 설비 모델은 같은 필터 표현이야.

값은 문자열 하나를 받고 정확히 일치하는 조건으로 조회해.
표준 컬럼은 EQP_MODEL이고 실제 데이터에서는 EQP_MODEL 또는 EQUIP_MODEL 컬럼으로 제공될 수 있어."""
    one_line = (
        "장비 모델 필터를 Main Flow Filter 메타데이터로 등록해줘. "
        "filter_key는 EQP_MODEL이고 status는 active야. 표시명은 장비 모델이야. "
        "장비모델, 장비 모델, 설비 모델은 같은 필터 표현이야. "
        "값은 문자열 하나를 받고 정확히 일치하는 조건으로 조회해. "
        "표준 컬럼은 EQP_MODEL이고 실제 데이터에서는 EQP_MODEL 또는 EQUIP_MODEL 컬럼으로 제공될 수 있어."
    )

    payload = refinement_normalizer.normalize_refinement(
        _filter_context(raw),
        {
            "refined_text": one_line,
            "resolved_references": [
                {"kind": "canonical_column", "input": "EQUIP_MODEL", "target": "EQP_MODEL"},
                {"kind": "main_filter", "input": "장비 모델", "target": "EQP_MODEL"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )

    refined = payload["metadata_authoring_draft"]["refined_text"]
    assert refined == """장비 모델 필터를 Main Flow Filter 메타데이터로 등록해줘.

filter_key는 EQP_MODEL이고 status는 active야.
표시명은 장비 모델이야.
장비모델, 장비 모델, 설비 모델은 같은 필터 표현이야.

값은 문자열 하나를 받고 정확히 일치하는 조건으로 조회해.
표준 컬럼은 EQP_MODEL이고 실제 데이터에서는 EQP_MODEL 또는 EQUIP_MODEL 컬럼으로 제공될 수 있어."""
    assert payload["refinement"]["refined_text"] == refined
    assert payload["trace"]["contract_resolution"]["refined_text_formatting"] == {
        "applied": True,
        "style": "readable_multiline_v1",
    }


def test_existing_compact_sql_line_is_formatted_without_changing_literals_or_placeholders() -> None:
    _, refinement_normalizer, *_ = _modules()
    raw = """검증 데이터셋을 Table Catalog 메타데이터로 등록해줘.
dataset_key는 validation_sample이고 status는 active야.

조회 SQL은 아래와 같아.

SELECT EQUIP_MODEL, RECIPE_ID FROM VALIDATION_SAMPLE WHERE STATUS_NM = 'ACTIVE  USER' AND EQUIP_MODEL = {EQP_MODEL}"""
    payload = refinement_normalizer.normalize_refinement(
        _table_context(raw),
        {
            "refined_text": raw,
            "resolved_references": [],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )

    refined = payload["metadata_authoring_draft"]["refined_text"]
    assert "SELECT\n    EQUIP_MODEL, RECIPE_ID" in refined
    assert "\nFROM VALIDATION_SAMPLE\nWHERE STATUS_NM = 'ACTIVE  USER'" in refined
    assert "EQUIP_MODEL = {EQP_MODEL}" in refined
    assert "```" not in refined

    sql_with_comment = "SELECT VALUE_NM -- FROM과 WHERE는 설명 주석\nFROM SAMPLE_TABLE WHERE VALUE_NM = 'A  B'"
    formatted_sql = refinement_normalizer._format_sql(sql_with_comment)
    assert "-- FROM과 WHERE는 설명 주석\nFROM SAMPLE_TABLE" in formatted_sql
    assert "'A  B'" in formatted_sql


def test_extraction_addendum_requires_one_item_for_one_declared_identity() -> None:
    addendum = (REV2_ROOT / "05_metadata_authoring_extraction_addendum_ko.md").read_text(encoding="utf-8")
    assert validate_prompt(addendum) == []
    assert "items는 정확히 1건만 반환한다" in addendum
    assert "별도 quantity_terms 또는 metric_terms item으로 분리하지 않는다" in addendum
    assert "join_keys는" in addendum
    assert "표준 컬럼 문자열 배열" in addendum
    assert "결측값을 0으로 계산한다는 명시가 없으면 propagate" in addendum
    assert "filter_mappings의 왼쪽에는 표준 컬럼" in addendum
    assert "default_detail_columns는 사용자가 기본 표시" in addendum
    assert validate_prompt(addendum) == []


def test_context_and_refinement_keep_original_and_resolve_registered_contracts() -> None:
    _, refinement_normalizer, *_ = _modules()
    raw = "장비 UPH 테이블과 장비 Assign 현황을 사용하고 장비모델, Recipe, 공정으로 결합해."
    payload = _context(raw)
    prompt_context = payload["metadata_authoring_context"]["prompt_context"]

    assert {item["dataset_key"] for item in prompt_context["datasets"]} >= {"equipment_assign", "eqp_uph"}
    assert {item["key"] for item in prompt_context["canonical_columns"]} >= {"EQP_MODEL", "RECIPE_ID", "OPER_NAME"}

    refined = refinement_normalizer.normalize_refinement(
        payload,
        {
            "refined_text": "equipment_assign와 eqp_uph를 EQP_MODEL, RECIPE_ID, OPER_NAME 기준으로 결합한다.",
            "resolved_references": [
                {"kind": "dataset", "input": "장비 UPH 테이블", "target": "eqp_uph"},
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
                {"kind": "canonical_column", "input": "장비모델", "target": "EQP_MODEL"},
                {"kind": "canonical_column", "input": "Recipe", "target": "RECIPE_ID"},
                {"kind": "canonical_column", "input": "공정", "target": "OPER_NAME"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )

    draft = refined["metadata_authoring_draft"]
    assert draft["original_text"] == raw
    assert "equipment_assign" in draft["refined_text"]
    assert "eqp_uph" in draft["refined_text"]
    assert draft["needs_more_input"] is False
    assert {item["target"] for item in draft["resolved_references"]} >= {
        "equipment_assign",
        "eqp_uph",
        "EQP_MODEL",
        "RECIPE_ID",
        "OPER_NAME",
    }


def test_refinement_adds_declared_identity_when_llm_omits_it() -> None:
    _, refinement_normalizer, *_ = _modules()
    raw = """장비 대수 계산 기준을 등록해줘.
section은 quantity_terms이고 key는 equipment_count이며 status는 active야."""
    payload = refinement_normalizer.normalize_refinement(
        _context(raw),
        {
            "refined_text": "장비 번호를 중복 없이 세어 장비 대수를 계산하는 기준을 등록해줘.",
            "resolved_references": [],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )

    refined = payload["metadata_authoring_draft"]["refined_text"]
    assert refined.startswith("장비 번호를 중복 없이 세어")
    assert "section은 quantity_terms, key는 equipment_count, status는 active로 등록해." in refined


def test_refinement_fails_closed_when_one_phrase_matches_multiple_registered_datasets() -> None:
    _, refinement_normalizer, extraction_variables, *_ = _modules()
    tables = _tables() + [
        _table(
            "eqp_uph_history",
            "Equipment UPH History",
            ["장비 UPH 테이블"],
            {"EQP_MODEL": ["EQUIP_MODEL"], "UPH": ["UPH"]},
        )
    ]
    payload = _context("장비 UPH 테이블을 사용해줘.", tables)
    refined = refinement_normalizer.normalize_refinement(
        payload,
        {
            "refined_text": "eqp_uph를 사용한다.",
            "resolved_references": [{"kind": "dataset", "input": "장비 UPH 테이블", "target": "eqp_uph"}],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )

    assert refined["metadata_authoring_draft"]["needs_more_input"] is True
    assert refined["metadata_authoring_draft"]["unresolved_references"][0]["candidates"] == [
        "eqp_uph",
        "eqp_uph_history",
    ]
    assert any(error["type"] == "ambiguous_metadata_reference" for error in refined["errors"])
    extraction_text = extraction_variables.build_extraction_text(refined)
    assert extraction_text.startswith("[REV_2 저장 보류]")
    assert "items는 빈 배열" in extraction_text


def test_reported_equipment_count_input_resolves_each_phrase_and_drops_incidental_references() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, *_ = _modules()
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    raw = """장비 대수 계산 기준을 도메인 메타데이터로 등록해줘.

section은 quantity_terms이고 key는 equipment_count이며 status는 active야.
장비 대수, 설비 대수, 장비 수, 몇 대는 같은 의미야.

장비 대수는 장비 Assign 현황 데이터에서 장비 번호를 중복 없이 센 값이야.
UPH를 함께 요청하지 않은 경우에는 장비 Assign 현황 데이터만 사용해."""
    live_like_tables = [
        _table(
            "equipment_assign",
            "Equipment Assign현황",
            [],
            {"EQP_ID": ["EQUIP_ID"], "EQP_MODEL": ["EQUIP_MODEL"]},
        ),
        _table(
            "eqp_uph",
            "Equipment UPH",
            [],
            {"EQP_MODEL": ["EQUIP_MODEL"], "UPH": ["UPH"]},
        ),
    ]
    live_like_filters = [
        {
            "filter_key": "EQP_ID",
            "status": "active",
            "payload": {"display_name": "장비 ID", "aliases": ["설비 ID"], "column_candidates": ["EQP_ID", "EQUIP_ID"]},
        },
        *_filters(),
    ]
    payload = _context(raw, live_like_tables, _domains(), live_like_filters)
    assert payload["metadata_authoring_context"]["declared_identity"] == {
        "section": "quantity_terms",
        "key": "equipment_count",
        "status": "active",
    }
    assert {item["dataset_key"] for item in payload["metadata_authoring_context"]["candidates"]["datasets"]} == {
        "equipment_assign",
        "eqp_uph",
    }
    assert payload["metadata_authoring_context"]["prompt_context"]["domains"] == []

    refined = refinement_normalizer.normalize_refinement(
        payload,
        {
            "refined_text": (
                "장비 대수 계산 기준을 도메인 메타데이터(section: quantity_terms, "
                "key: equipment_count, status: active)로 등록한다.\n"
                "장비 Assign 현황은 equipment_assign을 사용하고 장비 번호는 EQP_ID를 nunique한다.\n"
                "UPH가 함께 요청되지 않으면 equipment_assign만 사용한다."
            ),
            "resolved_references": [
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
                {"kind": "canonical_column", "input": "장비 번호", "target": "EQP_ID"},
                {"kind": "canonical_column", "input": "UPH", "target": "UPH"},
                {"kind": "domain", "input": "equipment_count", "target": "quantity_terms:equipment_count"},
                {"kind": "domain", "input": "UPH", "target": "analysis_recipes:uph_result_policy"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    draft = refined["metadata_authoring_draft"]
    assert draft["needs_more_input"] is False
    assert [(item["kind"], item["input"], item["target"]) for item in draft["resolved_references"]] == [
        ("dataset", "장비 Assign 현황", "equipment_assign"),
        ("canonical_column", "장비 번호", "EQP_ID"),
    ]
    assert "[확정된 기존 메타데이터 참조]" not in draft["refined_text"]
    assert "[저장 전 확인이 필요한 참조]" not in draft["refined_text"]

    extraction_response = {
        "items": [
            {
                "section": "quantity_terms",
                "key": "equipment_assign_count",
                "status": "active",
                "payload": {
                    "display_name": "장비 대수",
                    "aliases": ["장비 대수", "설비 대수", "장비 수", "몇 대"],
                    "data_source": "equipment_assign",
                    "columns": ["EQP_ID"],
                    "aggregation_method": "nunique",
                    "selection_criteria": ["UPH를 함께 요청하지 않은 경우에는 equipment_assign만 사용한다."],
                },
            }
        ],
        "missing_information": [],
        "assumptions": [],
    }
    repaired = candidate_repair.repair_candidate_response(refined, extraction_response)
    normalized = domain_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    guarded = guard.guard_metadata_contract(normalized)
    assert guarded["errors"] == []
    assert guarded["items"][0]["section"] == "quantity_terms"
    assert guarded["items"][0]["key"] == "equipment_count"
    assert guarded["items"][0]["status"] == "active"
    assert guarded["metadata_authoring_draft"]["retry_example"] == ""
    assert guarded["trace"]["declared_identity_lock"]["corrections"] == [
        {"field": "key", "from": "equipment_assign_count", "to": "equipment_count"}
    ]


def test_retry_guidance_is_full_copy_ready_natural_text_and_candidates_are_separate() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, response_enricher, message_adapter = _modules()
    raw = """장비 대수 계산 기준을 도메인 메타데이터로 등록해줘.
section은 quantity_terms이고 key는 equipment_count이며 status는 active야.
장비 대수는 장비 Assign 현황 데이터에서 장비 번호를 중복 없이 센 값이야."""
    ambiguous_tables = [
        _table("equipment_assign", "Equipment Assign", ["장비 Assign 현황"], {"EQP_ID": ["EQUIP_ID"]}),
        _table("equipment_assign_archive", "Equipment Assign Archive", ["장비 Assign 현황"], {"EQP_ID": ["EQUIP_ID"]}),
    ]
    payload = _context(raw, ambiguous_tables, _domains())
    refined = refinement_normalizer.normalize_refinement(
        payload,
        {
            "refined_text": "equipment_assign을 사용해 장비 대수를 계산하는 규칙으로 등록해줘.",
            "resolved_references": [
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"}
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    assert refined["metadata_authoring_draft"]["refined_text"] == raw
    assert refined["trace"]["contract_resolution"]["refined_text_source"] == "original_text_fallback"
    attempted = candidate_repair.repair_candidate_response(
        refined,
        {
            "items": [
                {
                    "section": "quantity_terms",
                    "key": "equipment_assign_count",
                    "status": "active",
                    "payload": {"data_source": "equipment_assign", "columns": ["EQP_ID"], "aggregation_method": "nunique"},
                }
            ],
            "missing_information": [],
            "assumptions": [],
        },
    )
    assert json.loads(attempted["llm_response"])["items"] == []
    assert attempted["payload"]["trace"]["authoring_candidate_repair"]["suppressed_item_count"] == 1

    guarded = guard.guard_metadata_contract(attempted["payload"])
    examples = guarded["metadata_authoring_draft"]["retry_examples"]
    assert len(examples) == 2
    assert all(example.startswith(raw) for example in examples)
    assert all("key는 equipment_count" in example for example in examples)
    assert all("equipment_assign_count" not in example for example in examples)
    assert all("은(는)" not in example and "아래 미확정 정보" not in example for example in examples)
    assert any("실제 dataset_key는 equipment_assign이야" in example for example in examples)
    assert any("실제 dataset_key는 equipment_assign_archive이야" in example for example in examples)
    assert guarded["items"] == []

    response = {
        "metadata_type": "domain",
        "status": "needs_input",
        "success": False,
        "message": "보완 정보가 필요합니다.",
        "answer_sections": {"summary": {"headline": "보완 정보가 필요합니다."}, "key_points": [], "notices": [], "next_steps": []},
        "metadata_authoring": {},
        "trace": {},
        "data": {"columns": [], "rows": [], "row_count": 0},
    }
    enriched = response_enricher.enrich_response(response, guarded)
    message = message_adapter.build_message(enriched)
    assert enriched["metadata_authoring"]["retry_example"] == examples[0]
    assert enriched["metadata_authoring"]["retry_examples"] == examples
    assert "#### 선택안 1" in message
    assert "#### 선택안 2" in message


def test_contract_guard_canonicalizes_domain_references_without_adding_item_schema_fields() -> None:
    _, refinement_normalizer, _, _, guard, *_ = _modules()
    raw = "장비 Assign 현황과 장비 UPH 테이블을 장비모델, Recipe, 공정으로 결합해."
    payload = refinement_normalizer.normalize_refinement(
        _context(raw),
        {
            "refined_text": raw,
            "resolved_references": [],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    payload["items"] = [
        {
            "section": "analysis_recipes",
            "key": "equipment_assignment_uph_join",
            "status": "active",
            "payload": {
                "display_name": "장비 배정과 UPH 결합",
                "source_datasets": ["장비 Assign 현황", "장비 UPH 테이블"],
                "join_type": "left",
                "join_keys": ["장비모델", "Recipe", "공정"],
                "preserve_left_rows": True,
            },
        }
    ]
    guarded = guard.guard_metadata_contract(payload)
    item = guarded["items"][0]

    assert item["payload"]["source_datasets"] == ["equipment_assign", "eqp_uph"]
    assert item["payload"]["join_keys"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
    assert set(item) == {"section", "key", "status", "payload"}
    assert not any(key.startswith("metadata_authoring") for key in item)
    assert guarded["errors"] == []
    assert guarded["metadata_authoring_draft"]["contract_validation"]["status"] == "validated"


def test_reported_join_rule_object_pairs_reach_successful_dry_run_with_existing_shape() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, response_enricher, message_adapter = _modules()
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    response_builder = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "08_domain_saving_response_builder.py")
    raw = """장비 배정과 UPH 결합 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 equipment_assignment_uph_join이며 status는 active야.
장비 Assign 현황을 기준으로 장비 UPH 테이블을 left join해.

결합 기준은 장비모델, Recipe, 공정이야.
장비 Assign 현황의 행은 모두 유지하고 장비 UPH 테이블에서는 UPH 값만 가져와."""
    refined_text = """장비 배정과 UPH 결합 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 equipment_assignment_uph_join이며 status는 active야.
equipment_assign(장비 Assign현황) 데이터셋을 기준으로 eqp_uph(Equipment UPH) 데이터셋을 left join해.

결합 기준은 EQP_MODEL(장비 모델), RECIPE_ID(Recipe ID), OPER_NAME(공정명)이야.
equipment_assign(장비 Assign현황) 데이터셋의 행은 모두 유지하고 eqp_uph(Equipment UPH) 데이터셋에서는 UPH 값만 가져와."""
    payload = refinement_normalizer.normalize_refinement(
        _context(raw),
        {
            "refined_text": refined_text,
            "resolved_references": [
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
                {"kind": "dataset", "input": "장비 UPH 테이블", "target": "eqp_uph"},
                {"kind": "canonical_column", "input": "장비모델", "target": "EQP_MODEL"},
                {"kind": "canonical_column", "input": "Recipe", "target": "RECIPE_ID"},
                {"kind": "canonical_column", "input": "공정", "target": "OPER_NAME"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    extraction_response = {
        "items": [
            {
                "section": "analysis_recipes",
                "key": "equipment_assignment_uph_join",
                "status": "active",
                "payload": {
                    "display_name": "장비 배정과 UPH 결합",
                    "source_datasets": ["equipment_assign", "eqp_uph"],
                    "join_mode": "row_enrichment",
                    "join_type": "left",
                    "join_keys": [
                        {"left_key": "EQP_MODEL", "right_key": "EQP_MODEL"},
                        {"left_key": "RECIPE_ID", "right_key": "RECIPE_ID"},
                        {"left_key": "OPER_NAME", "right_key": "OPER_NAME"},
                    ],
                    "right_value_columns": ["UPH"],
                    "preserve_left_rows": True,
                },
            }
        ],
        "missing_information": [],
        "assumptions": [],
    }

    repaired = candidate_repair.repair_candidate_response(payload, extraction_response)
    normalized = domain_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    guarded = guard.guard_metadata_contract(normalized)

    assert guarded["errors"] == []
    assert len(guarded["items"]) == 1
    body = guarded["items"][0]["payload"]
    assert body["join_keys"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]
    assert body["source_datasets"] == ["equipment_assign", "eqp_uph"]
    assert body["join_mode"] == "row_enrichment"
    assert body["join_type"] == "left"
    assert body["right_value_columns"] == ["UPH"]
    assert body["preserve_left_rows"] is True
    assert guarded["metadata_authoring_draft"]["retry_example"] == ""
    assert guarded["refinement"]["needs_more_input"] is False

    reviewed = writer.review_and_write(guarded)
    assert reviewed["review"]["ready_to_save"] is True
    assert reviewed["write_result"]["success"] is True
    assert reviewed["write_result"]["dry_run"] is True
    assert reviewed["write_result"]["would_save_count"] == 1

    response = response_builder.build_response(reviewed)
    enriched = response_enricher.enrich_response(response, reviewed)
    message = message_adapter.build_message(enriched)
    assert enriched["status"] == "dry_run"
    assert enriched["metadata_authoring"]["retry_examples"] == []
    assert "도메인 메타데이터 1건을 저장 전 검토했습니다" in message
    assert "### 이렇게 다시 입력해 보세요" not in message
    assert "{'left_key'" not in message


def test_object_join_pairs_resolve_physical_aliases_and_mismatch_fails_without_retry_dict_text() -> None:
    *_, guard, _, _ = _modules()

    def guarded_for(join_keys: list[dict]) -> dict:
        payload = _context("장비 Assign 현황과 장비 UPH 테이블을 결합하는 규칙을 등록해줘.")
        payload["metadata_authoring_draft"] = {
            "original_text": payload["request"]["raw_text"],
            "refined_text": payload["request"]["raw_text"],
            "resolved_references": [],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        }
        payload["items"] = [
            {
                "section": "analysis_recipes",
                "key": "join_contract",
                "status": "active",
                "payload": {
                    "source_datasets": ["equipment_assign", "eqp_uph"],
                    "join_keys": join_keys,
                },
            }
        ]
        return guard.guard_metadata_contract(payload)

    compatible = guarded_for(
        [
            {"left_key": "EQUIP_MODEL", "right_key": "EQP_MODEL"},
            {"left_on": "RECIPE_ID", "right_on": "RECIPE_ID"},
            {"canonical_key": "공정"},
        ]
    )
    assert compatible["errors"] == []
    assert compatible["items"][0]["payload"]["join_keys"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]

    mismatch = guarded_for([{"left_key": "EQP_MODEL", "right_key": "RECIPE_ID"}])
    assert [error["type"] for error in mismatch["errors"]] == ["join_key_pair_canonical_mismatch"]
    assert mismatch["metadata_authoring_draft"]["retry_example"] == ""
    assert not any("{'left_key'" in str(error.get("message") or "") for error in mismatch["errors"])


def test_candidate_repair_lowers_aggregate_operators_and_keeps_only_arithmetic_derived_metrics() -> None:
    _, _, _, candidate_repair, *_ = _modules()
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    payload = _context("EQP_ID 고유 장비 수와 평균 UPH로 보유 CAPA를 계산해.")
    response = {
        "items": [
            {
                "section": "analysis_recipes",
                "key": "held_capacity",
                "status": "active",
                "payload": {
                    "derived_metrics": [
                        {"output_column": "EQP_COUNT", "operator": "nunique", "operands": [{"column": "EQP_ID"}]},
                        {"output_column": "AVG_UPH", "operator": "avg", "operands": [{"column": "UPH"}]},
                        {
                            "output_column": "AVAILABLE_CAPA",
                            "operator": "multiply",
                            "operands": [{"column": "EQP_COUNT"}, {"column": "AVG_UPH"}, {"constant": 24}],
                            "null_policy": "propagate",
                        },
                    ]
                },
            }
        ],
        "missing_information": [],
        "assumptions": [],
    }
    repaired = candidate_repair.repair_candidate_response(payload, response)
    repaired_json = json.loads(repaired["llm_response"])
    body = repaired_json["items"][0]["payload"]

    assert [item["output_column"] for item in body["derived_metrics"]] == ["AVAILABLE_CAPA"]
    assert body["selection_criteria"] == [
        "집계 결과 EQP_COUNT은(는) EQP_ID 컬럼을 nunique 방식으로 집계한다.",
        "집계 결과 AVG_UPH은(는) UPH 컬럼을 mean 방식으로 집계한다.",
    ]
    assert repaired["payload"]["trace"]["authoring_candidate_repair"]["repair_count"] == 2
    normalized = domain_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    assert not any(error["type"] == "unsupported_derived_metric_operator" for error in normalized["errors"])
    assert normalized["items"][0]["payload"]["derived_metrics"][0]["operator"] == "multiply"


def test_reported_capa_invalid_generated_null_policy_defaults_to_propagate_and_reaches_dry_run() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, response_enricher, message_adapter = _modules()
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "07_domain_review_writer.py")
    response_builder = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "08_domain_saving_response_builder.py")
    raw = """보유 CAPA 계산 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 eqp_capacity_calculation이며 status는 active야.

장비 Assign 현황과 장비 UPH 테이블을 사용해.
장비모델, Recipe, 공정을 기준으로 두 데이터를 결합해.

장비 보유 대수는 장비 Assign 현황의 장비 번호를 중복 없이 센 값이야.
평균 UPH는 장비 UPH 테이블의 UPH를 평균낸 값이야.
보유 CAPA는 장비 보유 대수 × 평균 UPH × 24시간으로 계산해.

평균과 중복 제거는 각 입력 지표의 집계 기준이고 보유 CAPA 계산만 산술식이야."""
    eqp_id_filter = {
        "filter_key": "EQP_ID",
        "status": "active",
        "payload": {"display_name": "장비 ID", "aliases": ["장비 번호"], "column_candidates": ["EQP_ID", "EQUIP_ID"]},
    }
    payload = refinement_normalizer.normalize_refinement(
        _context(raw, filters=[eqp_id_filter, *_filters()]),
        {
            "refined_text": raw,
            "resolved_references": [
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
                {"kind": "dataset", "input": "장비 UPH", "target": "eqp_uph"},
                {"kind": "canonical_column", "input": "장비모델", "target": "EQP_MODEL"},
                {"kind": "canonical_column", "input": "Recipe", "target": "RECIPE_ID"},
                {"kind": "canonical_column", "input": "공정", "target": "OPER_NAME"},
                {"kind": "canonical_column", "input": "장비 번호", "target": "EQP_ID"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    extraction_response = {
        "items": [
            {
                "section": "analysis_recipes",
                "key": "eqp_capacity_calculation",
                "status": "active",
                "payload": {
                    "display_name": "보유 CAPA 계산",
                    "source_datasets": ["equipment_assign", "eqp_uph"],
                    "join_keys": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "derived_metrics": [
                        {"output_column": "EQP_COUNT", "operator": "nunique", "operands": [{"column": "EQP_ID"}]},
                        {"output_column": "AVG_UPH", "operator": "mean", "operands": [{"column": "UPH"}]},
                        {
                            "output_column": "AVAILABLE_CAPA",
                            "operator": "multiply",
                            "operands": [{"column": "EQP_COUNT"}, {"column": "AVG_UPH"}, {"constant": 24}],
                            "null_policy": "preserve_nulls",
                        },
                    ],
                },
            }
        ],
        "missing_information": [],
        "assumptions": [],
    }

    repaired = candidate_repair.repair_candidate_response(payload, extraction_response)
    repaired_json = json.loads(repaired["llm_response"])
    repaired_body = repaired_json["items"][0]["payload"]
    assert [metric["operator"] for metric in repaired_body["derived_metrics"]] == ["multiply"]
    assert repaired_body["derived_metrics"][0]["null_policy"] == "propagate"
    assert any(
        repair.get("field") == "null_policy"
        and repair.get("from") == "preserve_nulls"
        and repair.get("to") == "propagate"
        and repair.get("reason") == "default_when_not_user_specified"
        for repair in repaired["payload"]["trace"]["authoring_candidate_repair"]["repairs"]
    )

    normalized = domain_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    assert not any(error["type"] == "invalid_derived_metric_null_policy" for error in normalized["errors"])
    guarded = guard.guard_metadata_contract(normalized)
    assert guarded["errors"] == []
    assert len(guarded["items"]) == 1
    assert guarded["items"][0]["payload"]["derived_metrics"] == [
        {
            "output_column": "AVAILABLE_CAPA",
            "operator": "multiply",
            "operands": [{"column": "EQP_COUNT"}, {"column": "AVG_UPH"}, {"constant": 24}],
            "null_policy": "propagate",
        }
    ]

    reviewed = writer.review_and_write(guarded)
    assert reviewed["review"]["ready_to_save"] is True
    assert reviewed["write_result"]["success"] is True
    assert reviewed["write_result"]["dry_run"] is True
    assert reviewed["write_result"]["would_save_count"] == 1
    response = response_builder.build_response(reviewed)
    enriched = response_enricher.enrich_response(response, reviewed)
    message = message_adapter.build_message(enriched)
    assert enriched["status"] == "dry_run"
    assert "null_policy는 zero 또는 propagate만" not in message
    assert "MongoDB 설정 또는 입력 메타데이터" not in message
    assert "### 이렇게 다시 입력해 보세요" not in message

    explicit_zero = _context("보유 CAPA를 계산할 때 결측값은 0으로 계산해.")
    explicit_zero_result = candidate_repair.repair_candidate_response(
        explicit_zero,
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "held_capacity_zero_nulls",
                    "status": "active",
                    "payload": {
                        "derived_metrics": [
                            {
                                "output_column": "AVAILABLE_CAPA",
                                "operator": "multiply",
                                "operands": [{"column": "EQP_COUNT"}, {"column": "AVG_UPH"}],
                                "null_policy": "preserve",
                            }
                        ]
                    },
                }
            ]
        },
    )
    assert json.loads(explicit_zero_result["llm_response"])["items"][0]["payload"]["derived_metrics"][0]["null_policy"] == "zero"


def test_rev2_contract_error_next_step_does_not_blame_mongodb_configuration() -> None:
    *_, response_enricher, _ = _modules()
    error = {
        "type": "invalid_derived_metric_null_policy",
        "message": "null_policy는 zero 또는 propagate만 사용할 수 있습니다.",
    }
    response = {
        "status": "error",
        "success": False,
        "message": "도메인 메타데이터 저장 처리 중 문제가 발생했습니다.",
        "answer_sections": {
            "summary": {"headline": "도메인 메타데이터 저장 처리 중 문제가 발생했습니다."},
            "key_points": [],
            "notices": [{"type": "error", "title": "오류", "message": error["message"]}],
            "next_steps": ["오류 메시지를 확인하고 MongoDB 설정 또는 입력 메타데이터를 수정하세요."],
        },
        "write_result": {"status": "error", "errors": [error]},
        "trace": {"errors": [error]},
    }
    authoring = {
        "request": {"raw_text": "보유 CAPA 계산 규칙을 등록해줘."},
        "errors": [error],
        "write_result": {"status": "error", "errors": [error]},
    }

    enriched = response_enricher.enrich_response(response, authoring)
    next_steps = enriched["answer_sections"]["next_steps"]
    assert not any("MongoDB 설정" in step for step in next_steps)
    assert any("메타데이터 계약 검증" in step for step in next_steps)


def test_capa_request_selects_single_declared_recipe_when_model_splits_four_items() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, *_ = _modules()
    domain_normalizer = load_module(ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py")
    raw = """보유 CAPA 계산 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 eqp_capacity_calculation이며 status는 active야.

장비 Assign 현황과 장비 UPH 테이블을 사용해.
장비모델, Recipe, 공정을 기준으로 두 데이터를 결합해.

장비 보유 대수는 장비 Assign 현황의 장비 번호를 중복 없이 센 값이야.
평균 UPH는 장비 UPH 테이블의 UPH를 평균낸 값이야.
보유 CAPA는 장비 보유 대수 × 평균 UPH × 24시간으로 계산해.

    평균, 합계, 중복 제거는 집계 기준이고 보유 CAPA 계산만 산술식이야."""
    payload = refinement_normalizer.normalize_refinement(
        _context(
            raw,
            filters=[
                {
                    "filter_key": "EQP_ID",
                    "status": "active",
                    "payload": {"display_name": "장비 번호", "aliases": ["장비 ID"], "column_candidates": ["EQP_ID", "EQUIP_ID"]},
                },
                *_filters(),
            ],
        ),
        {
            "refined_text": raw,
            "resolved_references": [
                {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
                {"kind": "dataset", "input": "장비 UPH", "target": "eqp_uph"},
                {"kind": "canonical_column", "input": "장비모델", "target": "EQP_MODEL"},
                {"kind": "canonical_column", "input": "Recipe", "target": "RECIPE_ID"},
                {"kind": "canonical_column", "input": "공정", "target": "OPER_NAME"},
                {"kind": "canonical_column", "input": "장비 번호", "target": "EQP_ID"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    split_response = {
        "items": [
            {
                "section": "analysis_recipes",
                "key": "eqp_capacity_calculation",
                "status": "active",
                "payload": {
                    "display_name": "보유 CAPA 계산",
                    "source_datasets": ["equipment_assign", "eqp_uph"],
                    "join_keys": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "derived_metrics": [
                        {"output_column": "EQP_COUNT", "operator": "nunique", "operands": [{"column": "EQP_ID"}]},
                        {"output_column": "AVG_UPH", "operator": "mean", "operands": [{"column": "UPH"}]},
                        {
                            "output_column": "AVAILABLE_CAPA",
                            "operator": "multiply",
                            "operands": [{"column": "EQP_COUNT"}, {"column": "AVG_UPH"}, {"constant": 24}],
                            "null_policy": "propagate",
                        },
                    ],
                },
            },
            {
                "section": "quantity_terms",
                "key": "equipment_count",
                "status": "active",
                "payload": {"display_name": "장비 보유 대수", "data_source": "equipment_assign", "columns": ["EQP_ID"], "aggregation_method": "nunique"},
            },
            {
                "section": "metric_terms",
                "key": "average_uph",
                "status": "active",
                "payload": {"display_name": "평균 UPH", "data_source": "eqp_uph", "columns": ["UPH"], "aggregation_method": "mean"},
            },
            {
                "section": "metric_terms",
                "key": "available_capacity",
                "status": "active",
                "payload": {"display_name": "보유 CAPA"},
            },
        ],
        "missing_information": [],
        "assumptions": [],
    }
    repaired = candidate_repair.repair_candidate_response(payload, split_response)
    repaired_json = json.loads(repaired["llm_response"])
    assert [(item["section"], item["key"]) for item in repaired_json["items"]] == [
        ("analysis_recipes", "eqp_capacity_calculation")
    ]
    selection = repaired["payload"]["trace"]["declared_identity_candidate_selection"]
    assert selection["suppressed_count"] == 3
    assert selection["selected"] == "analysis_recipes:eqp_capacity_calculation"

    normalized = domain_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    guarded = guard.guard_metadata_contract(normalized)
    assert guarded["errors"] == []
    assert len(guarded["items"]) == 1
    assert guarded["items"][0]["key"] == "eqp_capacity_calculation"
    assert [metric["operator"] for metric in guarded["items"][0]["payload"]["derived_metrics"]] == ["multiply"]
    assert guarded["metadata_authoring_draft"]["retry_example"] == ""


def test_contract_guard_builds_copy_ready_retry_example_for_aggregate_operator_error() -> None:
    *_, guard, response_enricher, message_adapter = _modules()
    payload = _context("보유 CAPA는 장비 대수와 평균 UPH를 사용해 계산해.")
    payload["metadata_authoring_draft"] = {
        "original_text": payload["request"]["raw_text"],
        "refined_text": payload["request"]["raw_text"],
        "resolved_references": [],
        "unresolved_references": [],
        "missing_information": [],
        "assumptions": [],
        "needs_more_input": False,
    }
    payload["items"] = [{"section": "analysis_recipes", "key": "held_capacity", "status": "active", "payload": {}}]
    payload["errors"].append(
        {
            "type": "unsupported_derived_metric_operator",
            "message": "파생 metric operator는 add, subtract, multiply, divide만 사용할 수 있습니다.",
            "path": "items[0].payload.derived_metrics[0].operator",
        }
    )
    guarded = guard.guard_metadata_contract(payload)
    retry = guarded["metadata_authoring_draft"]["retry_example"]
    assert "mean, sum, nunique는 산술식이 아니라" in retry
    assert "AVAILABLE_CAPA = EQP_COUNT × AVG_UPH × 24" in retry

    base_response = {
        "metadata_type": "domain",
        "status": "needs_input",
        "success": False,
        "message": "보완 정보가 필요합니다.",
        "answer_sections": {"summary": {"headline": "보완 정보가 필요합니다."}, "key_points": [], "notices": [], "next_steps": []},
        "metadata_authoring": {},
        "trace": {},
        "data": {"columns": [], "rows": [], "row_count": 0},
    }
    enriched = response_enricher.enrich_response(base_response, guarded)
    message = message_adapter.build_message(enriched)
    assert enriched["metadata_authoring"]["original_text"] == payload["request"]["raw_text"]
    assert enriched["metadata_authoring"]["retry_example"] == retry
    assert "### 사용자 원문" in message
    assert "### Flow 정제안" in message
    assert "### 이렇게 다시 입력해 보세요" in message


def test_retry_example_deduplicates_guidance_already_present_in_retried_input() -> None:
    *_, guard, _, _ = _modules()
    guidance = """mean, sum, nunique는 산술식이 아니라 각 입력 지표의 집계 기준으로 저장해.
EQP_COUNT는 EQP_ID를 nunique한 결과이고 AVG_UPH는 UPH를 mean한 결과야.
파생 계산은 AVAILABLE_CAPA = EQP_COUNT × AVG_UPH × 24로 저장해."""
    raw = f"보유 CAPA 계산 규칙을 등록해줘.\n\n{guidance}\n\n{guidance}"
    payload = _context(raw)
    payload["metadata_authoring_draft"] = {
        "original_text": raw,
        "refined_text": raw,
        "resolved_references": [],
        "unresolved_references": [],
        "missing_information": [],
        "assumptions": [],
        "needs_more_input": False,
    }
    payload["items"] = [{"section": "analysis_recipes", "key": "held_capacity", "status": "active", "payload": {}}]
    payload["errors"].append(
        {
            "type": "unsupported_derived_metric_operator",
            "message": "파생 metric operator는 add, subtract, multiply, divide만 사용할 수 있습니다.",
        }
    )
    guarded = guard.guard_metadata_contract(payload)
    retry = guarded["metadata_authoring_draft"]["retry_example"]
    assert retry.count("mean, sum, nunique는 산술식이 아니라") == 1
    assert retry.count("EQP_COUNT는 EQP_ID를 nunique한 결과") == 1
    assert retry.count("파생 계산은 AVAILABLE_CAPA") == 1


def test_retry_example_uses_llm_authored_text_without_forcing_example_style() -> None:
    *_, guard, _, _ = _modules()
    raw = """보유 CAPA 계산 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 eqp_capacity_calculation이며 status는 active야.

장비 Assign 현황과 장비 UPH 테이블을 사용해.
장비모델, Recipe, 공정을 기준으로 두 데이터를 결합해.

장비 보유 대수는 장비 Assign 현황의 장비 번호를 중복 없이 센 값이야.
평균 UPH는 장비 UPH 테이블의 UPH값을 평균낸 값이야.
보유 CAPA는 장비 보유 대수 × 평균 UPH × 24시간으로 계산해."""
    eqp_id_filter = {
        "filter_key": "EQP_ID",
        "status": "active",
        "payload": {
            "display_name": "장비 번호",
            "aliases": ["장비 ID"],
            "column_candidates": ["EQP_ID", "EQUIP_ID"],
        },
    }
    resolutions = [
        {"kind": "dataset", "input": "장비 Assign 현황", "target": "equipment_assign"},
        {"kind": "dataset", "input": "장비 UPH", "target": "eqp_uph"},
        {"kind": "canonical_column", "input": "장비모델", "target": "EQP_MODEL"},
        {"kind": "canonical_column", "input": "Recipe", "target": "RECIPE_ID"},
        {"kind": "canonical_column", "input": "공정", "target": "OPER_NAME"},
        {"kind": "canonical_column", "input": "장비 번호", "target": "EQP_ID"},
    ]
    ai_refined = """보유 CAPA 계산 규칙을 도메인 메타데이터로 등록해줘.

section은 analysis_recipes이고 key는 eqp_capacity_calculation이며 status는 active야.

사용 데이터셋은 equipment_assign과 eqp_uph야. 두 데이터는 EQP_MODEL, RECIPE_ID, OPER_NAME을 기준으로 결합해.
equipment_assign에서 EQP_ID의 고유 개수를 장비 보유 대수로 계산하고, eqp_uph에서 UPH 평균을 계산해.
보유 CAPA는 장비 보유 대수와 평균 UPH, 24시간을 곱해서 계산해."""

    def guarded_retry(source_text: str, refined_text: str) -> str:
        payload = _context(source_text, filters=[eqp_id_filter, *_filters()])
        payload["metadata_authoring_draft"] = {
            "original_text": source_text,
            "refined_text": refined_text,
            "resolved_references": resolutions,
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        }
        payload["items"] = [
            {
                "section": "analysis_recipes",
                "key": "eqp_capacity_calculation",
                "status": "active",
                "payload": {},
            }
        ]
        payload["errors"].append(
            {
                "type": "unsupported_derived_metric_operator",
                "message": "파생 metric operator는 add, subtract, multiply, divide만 사용할 수 있습니다.",
            }
        )
        result = guard.guard_metadata_contract(payload)
        return result["metadata_authoring_draft"]["retry_example"]

    retry = guarded_retry(raw, ai_refined)
    assert retry.startswith(ai_refined)
    assert "equipment_assign" in retry and "eqp_uph" in retry
    assert "EQP_MODEL" in retry and "RECIPE_ID" in retry and "OPER_NAME" in retry and "EQP_ID" in retry
    assert "(equipment_assign 테이블)" not in retry
    assert "(EQP_MODEL 컬럼)" not in retry
    assert "데이터셋 매핑에서" not in retry
    assert "mean, sum, nunique는 산술식이 아니라" in retry

    retry_again = guarded_retry(retry, retry)
    assert retry_again == retry

    fallback = guarded_retry(
        raw,
        "장비 배정 현황과 UPH 자료를 결합해 보유 CAPA를 계산하는 규칙을 등록해줘.",
    )
    assert "데이터셋 매핑에서 '장비 Assign 현황'의 dataset_key는 equipment_assign이야." in fallback
    assert "컬럼 매핑에서 '장비모델'의 표준 컬럼은 EQP_MODEL이야." in fallback


def test_reported_table_catalog_mapping_is_repaired_and_reaches_successful_dry_run() -> None:
    _, refinement_normalizer, _, candidate_repair, guard, response_enricher, message_adapter = _modules()
    table_normalizer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "04_table_catalog_saving_result_normalizer.py")
    writer = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "07_table_catalog_review_writer.py")
    response_builder = load_module(ROOT / "langflow_components" / "table_catalog_saving_flow" / "08_table_catalog_saving_response_builder.py")
    raw = """신규 장비 성능 검증 데이터를 Table Catalog 메타데이터로 등록해줘.

dataset_key는 eqp_performance_validation이고 status는 active야.
표시명은 신규 장비 성능 검증 데이터야.
Oracle 데이터이고 db_key는 GMS_DB야.

조회 SQL은 아래와 같아.

SELECT
    EQUIP_MODEL,
    RECIPE_ID,
    OPER_NM,
    AVG_UPH_VAL AS UPH
FROM EQP_PERFORMANCE_VALIDATION
WHERE EQUIP_MODEL = {EQP_MODEL}

사용자가 말하는 장비모델은 조회 결과의 EQUIP_MODEL과 연결해.
Recipe는 RECIPE_ID와 연결하고 공정은 OPER_NM과 연결해.
UPH값은 조회 결과의 UPH를 사용해.
장비모델은 필수 조회 조건이야.

이 데이터는 장비모델과 Recipe, 공정별 평균 UPH를 확인할 때 사용해."""
    refined_text = (
        "dataset_key는 eqp_performance_validation이고 status는 active인 신규 장비 성능 검증 데이터 "
        "Table Catalog 메타데이터를 등록한다. Oracle 데이터이며 db_key는 GMS_DB이다. "
        "조회 SQL은 'SELECT EQUIP_MODEL, RECIPE_ID, OPER_NM, AVG_UPH_VAL AS UPH "
        "FROM EQP_PERFORMANCE_VALIDATION WHERE EQUIP_MODEL = {EQP_MODEL}'이다. "
        "조회 결과의 EQUIP_MODEL은 EQP_MODEL(장비 모델) 컬럼과 연결하고, Recipe는 RECIPE_ID 컬럼과 "
        "연결하며, 공정은 OPER_NM(OPER_NAME) 컬럼과 연결하고, UPH값은 UPH 컬럼을 사용한다. "
        "장비모델은 필수 조회 조건이며, 이 데이터는 장비모델과 Recipe, 공정별 평균 UPH를 확인할 때 사용한다."
    )
    payload = refinement_normalizer.normalize_refinement(
        _table_context(raw),
        {
            "refined_text": refined_text,
            "resolved_references": [
                {"kind": "canonical_column", "input": "EQUIP_MODEL", "target": "EQP_MODEL"},
                {"kind": "canonical_column", "input": "OPER_NM", "target": "OPER_NAME"},
            ],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "needs_more_input": False,
        },
    )
    formatted_refined = payload["metadata_authoring_draft"]["refined_text"]
    assert "조회 SQL은 아래와 같다.\n\nSELECT\n" in formatted_refined
    assert "\nFROM EQP_PERFORMANCE_VALIDATION\nWHERE EQUIP_MODEL = {EQP_MODEL}" in formatted_refined
    assert "\n\n조회 결과의 EQUIP_MODEL은" in formatted_refined
    assert "```" not in formatted_refined
    assert payload["trace"]["contract_resolution"]["refined_text_formatting"]["applied"] is True
    extraction_response = {
        "items": [
            {
                "dataset_key": "eqp_performance_validation",
                "status": "active",
                "payload": {
                    "display_name": "신규 장비 성능 검증 데이터",
                    "dataset_family": "eqp_performance",
                    "source_type": "oracle",
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "GMS_DB",
                        "query_template": (
                            "SELECT EQUIP_MODEL, RECIPE_ID, OPER_NM, AVG_UPH_VAL AS UPH "
                            "FROM EQP_PERFORMANCE_VALIDATION WHERE EQUIP_MODEL = {EQP_MODEL}"
                        ),
                    },
                    "required_params": ["EQP_MODEL"],
                    "required_param_mappings": {"EQP_MODEL": ["EQUIP_MODEL"]},
                    # 보고된 약한 후보: OPER_NAME -> OPER_NM 실행 mapping을 누락했습니다.
                    "filter_mappings": {
                        "EQP_MODEL": ["EQUIP_MODEL"],
                        "RECIPE_ID": ["RECIPE_ID"],
                        "UPH": ["UPH"],
                    },
                    "columns": ["EQUIP_MODEL", "RECIPE_ID", "OPER_NM", "UPH"],
                    # 원문에 없는 필드를 모델이 사용 목적 문장에서 임의 생성한 재현값입니다.
                    "default_detail_columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
                    "selection_criteria": {
                        "use_when": ["장비모델과 Recipe, 공정별 평균 UPH를 확인할 때"]
                    },
                },
            }
        ],
        "missing_information": [],
        "assumptions": [],
    }

    repaired = candidate_repair.repair_candidate_response(payload, extraction_response)
    repaired_json = json.loads(repaired["llm_response"])
    repaired_body = repaired_json["items"][0]["payload"]
    assert repaired_body["filter_mappings"]["OPER_NAME"] == ["OPER_NM"]
    assert "default_detail_columns" not in repaired_body
    repairs = repaired["payload"]["trace"]["authoring_candidate_repair"]["repairs"]
    assert any(
        repair.get("action") == "add_explicit_resolved_mapping"
        and repair.get("canonical_column") == "OPER_NAME"
        and repair.get("source_column") == "OPER_NM"
        for repair in repairs
    )
    assert any(
        repair.get("action") == "remove_unrequested_optional_field"
        and repair.get("field") == "default_detail_columns"
        for repair in repairs
    )

    normalized = table_normalizer.normalize_authoring(repaired["payload"], repaired["llm_response"])
    guarded = guard.guard_metadata_contract(normalized)
    assert guarded["errors"] == []
    assert len(guarded["items"]) == 1
    item = guarded["items"][0]
    assert item["dataset_key"] == "eqp_performance_validation"
    assert item["payload"]["columns"] == ["EQUIP_MODEL", "RECIPE_ID", "OPER_NM", "UPH"]
    assert item["payload"]["filter_mappings"] == {
        "EQP_MODEL": ["EQUIP_MODEL"],
        "RECIPE_ID": ["RECIPE_ID"],
        "UPH": ["UPH"],
        "OPER_NAME": ["OPER_NM"],
    }
    assert "default_detail_columns" not in item["payload"]

    reviewed = writer.review_and_write(guarded)
    assert reviewed["review"]["ready_to_save"] is True
    assert reviewed["write_result"]["success"] is True
    assert reviewed["write_result"]["dry_run"] is True
    assert reviewed["write_result"]["would_save_count"] == 1
    response = response_builder.build_response(reviewed)
    enriched = response_enricher.enrich_response(response, reviewed)
    message = message_adapter.build_message(enriched)
    assert enriched["status"] == "dry_run"
    assert f"### Flow 정제안\n```text\n{formatted_refined}\n```" in message
    assert "default_detail_columns 컬럼이" not in message
    assert "MongoDB 설정 또는 데이터셋 정의" not in message
    assert "### 이렇게 다시 입력해 보세요" not in message

    explicit_default_payload = _table_context(
        "검증 테이블을 등록해줘. 기본 표시 컬럼은 EQP_MODEL과 OPER_NAME이야."
    )
    explicit_default_result = candidate_repair.repair_candidate_response(
        explicit_default_payload,
        {
            "items": [
                {
                    "dataset_key": "explicit_default_columns",
                    "status": "active",
                    "payload": {
                        "columns": ["EQP_MODEL", "OPER_NAME"],
                        "default_detail_columns": ["EQP_MODEL", "OPER_NAME"],
                    },
                }
            ]
        },
    )
    assert json.loads(explicit_default_result["llm_response"])["items"][0]["payload"]["default_detail_columns"] == [
        "EQP_MODEL",
        "OPER_NAME",
    ]


def test_contract_guard_canonicalizes_table_mapping_and_main_filter_key() -> None:
    *_, guard, _, _ = _modules()
    base = _context("장비모델은 표준 EQP_MODEL이고 실제 조회 컬럼은 EQUIP_MODEL이야.")
    base["metadata_authoring_draft"] = {
        "original_text": base["request"]["raw_text"],
        "refined_text": base["request"]["raw_text"],
        "resolved_references": [],
        "unresolved_references": [],
        "missing_information": [],
        "assumptions": [],
        "needs_more_input": False,
    }

    table_payload = dict(base)
    table_payload["metadata_type"] = "table_catalog"
    table_payload["items"] = [
        {
            "dataset_key": "new_equipment_table",
            "status": "active",
            "payload": {
                "display_name": "신규 장비 테이블",
                "filter_mappings": {"EQUIP_MODEL": ["EQUIP_MODEL"], "UPH": ["UPH"]},
                "standard_column_aliases": {"장비모델": ["EQUIP_MODEL"]},
                "columns": ["EQUIP_MODEL", "UPH"],
            },
        }
    ]
    guarded_table = guard.guard_metadata_contract(table_payload)
    table_body = guarded_table["items"][0]["payload"]
    assert table_body["filter_mappings"] == {"EQP_MODEL": ["EQUIP_MODEL"], "UPH": ["UPH"]}
    assert table_body["standard_column_aliases"] == {"EQP_MODEL": ["EQUIP_MODEL"]}
    assert guarded_table["errors"] == []

    filter_payload = dict(base)
    filter_payload["metadata_type"] = "main_flow_filter"
    filter_payload["items"] = [
        {
            "filter_key": "장비모델",
            "status": "active",
            "payload": {"display_name": "장비 모델", "aliases": ["장비모델"], "column_candidates": ["EQUIP_MODEL"]},
        }
    ]
    guarded_filter = guard.guard_metadata_contract(filter_payload)
    assert guarded_filter["items"][0]["filter_key"] == "EQP_MODEL"
    assert guarded_filter["errors"] == []


def test_contract_guard_blocks_unregistered_domain_dataset_and_returns_retry_example() -> None:
    *_, guard, _, _ = _modules()
    payload = _context("등록되지 않은 장비 집계 테이블을 사용해 장비 수를 계산해.")
    payload["metadata_authoring_draft"] = {
        "original_text": payload["request"]["raw_text"],
        "refined_text": payload["request"]["raw_text"],
        "resolved_references": [],
        "unresolved_references": [],
        "missing_information": [],
        "assumptions": [],
        "needs_more_input": False,
    }
    payload["items"] = [
        {
            "section": "quantity_terms",
            "key": "equipment_count",
            "status": "active",
            "payload": {"data_source": "등록되지 않은 장비 집계 테이블", "columns": ["EQP_ID"], "aggregation_method": "nunique"},
        }
    ]
    guarded = guard.guard_metadata_contract(payload)

    assert any(error["type"] == "unknown_dataset_reference" for error in guarded["errors"])
    assert guarded["refinement"]["needs_more_input"] is True
    assert guarded["metadata_authoring_draft"]["contract_validation"]["status"] == "needs_input"
    assert "실제 dataset_key를 명시" in guarded["metadata_authoring_draft"]["retry_example"]


def test_rev_2_builder_is_isolated_and_preserves_original_flow_files(tmp_path: Path) -> None:
    before = {path: _sha256(path) for path in ORIGINAL_FLOW_PATHS}
    result = write_rev_2_flows(tmp_path / "exports", tmp_path / "imports", tmp_path / "rev_2.zip")
    after = {path: _sha256(path) for path in ORIGINAL_FLOW_PATHS}

    assert before == after
    assert result["flow_count"] == 3
    assert result["canonical_bundle_unchanged"] is True
    assert result["router_targets_rev_2"] is False
    manifest = json.loads((tmp_path / "imports" / "manifest.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in manifest["flows"]] == list(REV2_DISPLAY_NAMES.values())
    assert all(item["nodes"] == 20 and item["edges"] == 28 for item in manifest["flows"])

    for item in manifest["flows"]:
        flow = json.loads((tmp_path / "imports" / item["file"]).read_text(encoding="utf-8"))
        assert flow["last_tested_version"] == "1.11.0"
        assert all(node["data"]["node"]["lf_version"] == "1.11.0" for node in flow["data"]["nodes"])
        node_ids = {node["id"] for node in flow["data"]["nodes"]}
        assert any(node_id.startswith("MetadataSnapshot-") for node_id in node_ids)
        assert any(node_id.startswith("RefinementNormalizer-") for node_id in node_ids)
        assert any(node_id.startswith("CandidateRepair-") for node_id in node_ids)
        assert any(node_id.startswith("ContractGuard-") for node_id in node_ids)
        assert any(node_id.startswith("ResponseEnricher-") for node_id in node_ids)
        slug = next(key for key, name in REV2_DISPLAY_NAMES.items() if name == item["name"])
        source_folder = {
            "domain": ROOT / "langflow_components" / "domain_saving_flow",
            "table_catalog": ROOT / "langflow_components" / "table_catalog_saving_flow",
            "main_flow_filter": ROOT / "langflow_components" / "main_flow_filters_saving_flow",
        }[slug]
        writer_file = {
            "domain": "07_domain_review_writer.py",
            "table_catalog": "07_table_catalog_review_writer.py",
            "main_flow_filter": "07_main_flow_filter_review_writer.py",
        }[slug]
        writer = next(node for node in flow["data"]["nodes"] if node["id"] == f"Writer-{slug}-rev-2")
        embedded_writer = writer["data"]["node"]["template"]["code"]["value"]
        assert embedded_writer == (source_folder / writer_file).read_text(encoding="utf-8")


def test_rev_2_source_export_and_import_artifacts_are_synchronized() -> None:
    result = audit_rev2_repository()
    assert result["status"] == "ok"
    assert [report["flow_count"] for report in result["reports"]] == [3, 3, 3]
    assert result["errors"] == []
