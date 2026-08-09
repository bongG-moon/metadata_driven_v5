from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> Any:
    name = f"test_table_catalog_natural_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


NORMALIZER = _load(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "04_table_catalog_saving_result_normalizer.py"
)
WRITER = _load(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "07_table_catalog_review_writer.py"
)
SIMILARITY = _load(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "05_table_catalog_similarity_checker.py"
)


NATURAL_HOLD_HISTORY_TEXT = """
HOLD 이력 조회 데이터는 hold_history로 등록해줘.
이 데이터는 Oracle 기반이고 db_key는 PNT_RPT야.
HOLD된 LOT의 이력과 상세 사유를 확인하는 데이터야.
LOT_ID가 조회 필수 조건이야.
HOLD LOT 건수는 LOT_ID의 UNIQUE 수량을 count해야 해.
HOLD DIE 수량이나 UNIT 수량은 OLD_SUB_PROD_QTY 값의 합을 구해야 해.
같은 세션에서 바로 앞에 조회한 LOT 목록이 있으면, 그 LOT 번호들을 사용해 이력도 이어서 조회할 수 있게 해줘.
한 번에는 최대 200개의 LOT만 조회하면 돼.
"""


def _hold_history_item(*, include_old_sub_prod_qty: bool = False) -> dict[str, Any]:
    columns = [
        "LOT_ID",
        "PROD_QTY",
        "OPER",
        "OPER_NAME",
        "HOLD_TM",
        "HOLD_CD",
        "HOLD_DESC",
    ]
    if include_old_sub_prod_qty:
        columns.append("OLD_SUB_PROD_QTY")
    query_columns = ", ".join(columns)
    return {
        "dataset_key": "hold_history",
        "status": "active",
        "payload": {
            "display_name": "HOLD History",
            "dataset_family": "hold",
            "source_type": "oracle",
            "source_config": {
                "source_type": "oracle",
                "db_key": "PNT_RPT",
                "query_template": f"SELECT {query_columns} FROM HOLD_HIS WHERE LOT_ID IN ({{LOT_ID}})",
            },
            "required_params": ["LOT_ID"],
            "required_param_mappings": {"LOT_ID": ["LOT_ID"]},
            "filter_mappings": {
                "LOT_ID": ["LOT_ID"],
                "OPER_NAME": ["OPER_NAME"],
            },
            "columns": columns,
            "default_detail_columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
            # 약한 모델이 업무 결과명을 실제 컬럼처럼 저장한 재현값
            "metric_semantics": {
                "HOLD_LOT_COUNT": {
                    "semantic_type": "count",
                    "additive": False,
                    "default_rollup": "nunique",
                    "allowed_rollups": ["nunique"],
                    "source_already_aggregated": False,
                },
                "HOLD_DIE_QTY": {
                    "semantic_type": "quantity",
                    "additive": True,
                    "default_rollup": "sum",
                    "allowed_rollups": ["sum"],
                    "source_already_aggregated": False,
                },
                "UNIT_QTY": {
                    "semantic_type": "quantity",
                    "additive": True,
                    "default_rollup": "sum",
                    "allowed_rollups": ["sum"],
                    "source_already_aggregated": False,
                },
            },
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "request": {"raw_text": NATURAL_HOLD_HISTORY_TEXT, "dry_run": True, "duplicate_action": "skip"},
        "refinement": {"refined_text": "", "needs_more_input": False, "missing_information": [], "assumptions": []},
        "errors": [],
    }


def test_natural_hold_history_authoring_folds_derived_metric_names_and_inferrs_previous_result_binding():
    normalized = NORMALIZER.normalize_authoring(_payload(), {"items": [_hold_history_item()]})

    item = normalized["items"][0]["payload"]
    assert item["metric_semantics"] == {
        "LOT_ID": {
            "semantic_type": "count",
            "additive": False,
            "default_rollup": "nunique",
            "allowed_rollups": ["nunique"],
            "source_already_aggregated": False,
        }
    }
    assert item["source_config"]["upstream_bindings"] == [
        {
            "entity_type": "lot",
            "source_alias": "previous_result",
            "source_column": "LOT_ID",
            "target_param": "LOT_ID",
            "operator": "in",
            "max_values": 200,
        }
    ]
    assert item["required_params"] == ["LOT_ID"]
    assumptions = normalized["refinement"]["assumptions"]
    assert any("OLD_SUB_PROD_QTY" in message for message in assumptions)

    reviewed = WRITER.review_and_write(normalized)
    assert reviewed["write_result"]["success"] is True
    assert reviewed["review"]["errors"] == []


def test_natural_metric_mapping_uses_explicit_quantity_column_only_when_query_returns_it():
    normalized = NORMALIZER.normalize_authoring(
        _payload(),
        {"items": [_hold_history_item(include_old_sub_prod_qty=True)]},
    )

    semantics = normalized["items"][0]["payload"]["metric_semantics"]
    assert semantics["LOT_ID"]["default_rollup"] == "nunique"
    assert semantics["OLD_SUB_PROD_QTY"] == {
        "semantic_type": "quantity",
        "additive": True,
        "default_rollup": "sum",
        "allowed_rollups": ["sum"],
        "source_already_aggregated": False,
    }
    assert not any("OLD_SUB_PROD_QTY" in message for message in normalized["refinement"]["assumptions"])


def test_natural_count_recovers_one_schema_backed_identifier_from_worker_wording():
    payload = _payload()
    payload["request"]["raw_text"] = """
    LOT 번호를 중복 없이 세면 HOLD LOT 건수야.
    query_template:
    SELECT LOT_ID, FLOW_ID, HOLD_TM FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})
    """
    item = _hold_history_item()
    item["payload"]["columns"].append("FLOW_ID")
    item["payload"]["metric_semantics"] = {
        "HOLD_LOT_COUNT": {
            "semantic_type": "count",
            "additive": False,
            "default_rollup": "nunique",
            "allowed_rollups": ["nunique"],
            "source_already_aggregated": False,
        }
    }

    normalized = NORMALIZER.normalize_authoring(payload, {"items": [item]})
    assert list(normalized["items"][0]["payload"]["metric_semantics"]) == ["LOT_ID"]


def test_natural_count_does_not_guess_between_unmentioned_identifier_columns():
    payload = _payload()
    payload["request"]["raw_text"] = """
    중복 없이 세면 건수야.
    query_template:
    SELECT LOT_ID, FLOW_ID FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})
    """
    item = _hold_history_item()
    item["payload"]["columns"].append("FLOW_ID")
    item["payload"]["metric_semantics"] = {}

    normalized = NORMALIZER.normalize_authoring(payload, {"items": [item]})
    assert normalized["items"][0]["payload"].get("metric_semantics") in ({}, None)


def test_previous_result_relation_after_a_query_template_is_not_lost_from_natural_text():
    payload = _payload()
    payload["request"]["raw_text"] = """
    HOLD 이력 데이터야.
    query_template:
    SELECT LOT_ID, HOLD_TM, HOLD_CD, HOLD_DESC FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})
    같은 세션에서 바로 앞에 조회한 LOT 목록이 있으면 그 LOT 번호로 이력도 이어서 조회할 수 있게 해줘.
    """

    normalized = NORMALIZER.normalize_authoring(payload, {"items": [_hold_history_item()]})
    assert normalized["items"][0]["payload"]["source_config"]["upstream_bindings"][0]["target_param"] == "LOT_ID"


def test_direct_identifier_wording_keeps_required_param_alongside_optional_previous_result_relation():
    payload = _payload()
    payload["request"]["raw_text"] = """
    이력 데이터는 LOT 번호를 직접 말하면 바로 조회할 수 있어.
    같은 세션에서 앞서 조회한 LOT 목록이 있으면 그 번호로도 이어서 조회할 수 있게 해줘.
    query_template:
    SELECT LOT_ID, HOLD_TM FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})
    """

    normalized = NORMALIZER.normalize_authoring(payload, {"items": [_hold_history_item()]})
    item = normalized["items"][0]["payload"]
    assert item["required_params"] == ["LOT_ID"]
    assert item["source_config"]["upstream_bindings"][0]["target_param"] == "LOT_ID"


def test_prompt_explicitly_accepts_natural_previous_result_relation_without_internal_field_names():
    prompt = (
        ROOT
        / "langflow_components"
        / "table_catalog_saving_flow"
        / "03_saving_prompt_template_ko.md"
    ).read_text(encoding="utf-8")
    assert "보조 입력 경로" in prompt

    assert "작업자는 내부 필드명" in prompt
    assert "결과에 붙일 이름을 key로 만들지 않는다" in prompt


def test_existing_catalog_usage_description_update_keeps_execution_contract_and_merges_selection_only():
    raw_text = """
    기존 장비 Assign 현황 데이터의 사용 설명만 바꿔줘. 데이터 키: equipment_assign.
    배정된 장비, 할당 장비, 장비 목록, 장비 대수, 장비 모델과 레시피 조합을 물어볼 때 사용하는 데이터야.
    UPH나 시간당 생산성을 물어볼 때만 장비 UPH 데이터를 함께 봐.
    """
    payload = {
        "request": {"raw_text": raw_text, "duplicate_action": "merge", "dry_run": True},
        "refinement": {"refined_text": "", "needs_more_input": False, "missing_information": [], "assumptions": []},
        "errors": [],
    }
    weak_model_item = {
        "dataset_key": "equipment_assign",
        "status": "active",
        "payload": {
            "source_type": "unknown",
            "source_config": {"source_type": "unknown", "query_template": ""},
            "columns": [],
            "selection_criteria": {
                "use_when": ["배정된 장비나 장비 목록을 물어볼 때"],
                "exclude_when": ["UPH를 물어볼 때"],
            },
        },
    }
    normalized = NORMALIZER.normalize_authoring(
        payload,
        [{"type": "text", "text": __import__("json").dumps({"items": [weak_model_item], "missing_information": ["schema needed"]})}],
    )

    item = normalized["items"][0]
    assert item["_partial_update"]["fields"] == ["selection_criteria"]
    assert item["payload"] == {
        "selection_criteria": {
            "use_when": ["배정된 장비나 장비 목록을 물어볼 때"],
            "exclude_when": ["UPH를 물어볼 때"],
        }
    }
    assert normalized["refinement"]["needs_more_input"] is False
    assert normalized["refinement"]["missing_information"] == []

    existing = {
        "_id": "table_catalog:equipment_assign",
        "dataset_key": "equipment_assign",
        "status": "active",
        "payload": {
            "source_type": "oracle",
            "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT EQUIP_ID FROM EQP"},
            "columns": ["EQUIP_ID"],
            "filter_mappings": {"EQP_ID": ["EQUIP_ID"]},
        },
    }
    matched = SIMILARITY.check_similarity(normalized, [existing])
    reviewed = WRITER.review_and_write(
        matched,
        {"ready_to_save": True, "errors": [], "supplement_requests": [], "assumptions": []},
    )
    result_item = reviewed["items"][0]
    assert reviewed["write_result"]["success"] is True
    assert result_item["payload"]["source_config"]["query_template"] == "SELECT EQUIP_ID FROM EQP"
    assert result_item["payload"]["selection_criteria"]["use_when"] == ["배정된 장비나 장비 목록을 물어볼 때"]
    assert reviewed["trace"]["partial_usage_update_materialization"]["status"] == "applied"


def test_partial_usage_update_without_exact_existing_catalog_fails_closed():
    payload = {
        "request": {"raw_text": "", "duplicate_action": "merge", "dry_run": True},
        "items": [{
            "dataset_key": "missing_dataset",
            "status": "active",
            "payload": {"selection_criteria": {"use_when": ["목록 조회"]}},
            "_partial_update": {"fields": ["selection_criteria"], "reason": "worker_usage_description"},
        }],
        "existing_matches": [],
        "errors": [],
    }
    reviewed = WRITER.review_and_write(
        payload,
        {"ready_to_save": True, "errors": [], "supplement_requests": [], "assumptions": []},
    )
    assert reviewed["write_result"]["success"] is False
    assert any(error["type"] == "partial_usage_update_existing_catalog_missing" for error in reviewed["review"]["errors"])
