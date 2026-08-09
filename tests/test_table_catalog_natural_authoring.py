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
