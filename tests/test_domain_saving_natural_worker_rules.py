from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path):
    name = f"test_domain_worker_rules_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


NORMALIZER = _load(
    ROOT / "langflow_components" / "domain_saving_flow" / "04_domain_saving_result_normalizer.py"
)


WORKER_EQUIPMENT_RULE = """
장비 대수와 장비 목록을 계산하는 기준을 등록해줘.
장비 대수, 설비 대수, 장비 수, 설비 수, 몇 대, 장비 목록, 장비 LIST는 장비 Assign 현황 데이터(equipment_assign)를 사용해.
장비 수는 장비 번호 EQP_ID를 중복 없이 세면 돼. 장비 목록은 같은 장비 번호를 중복 없이 보여줘.
UPH를 함께 물을 때만 장비 UPH 데이터를 추가로 사용하고, 장비 대수나 목록만 물을 때는 장비 Assign 현황 데이터만 사용해.
"""


def test_empty_llm_items_recover_only_explicit_worker_quantity_contract():
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": WORKER_EQUIPMENT_RULE}, "refinement": {}},
        {"items": []},
    )

    assert payload["errors"] == []
    item = payload["items"][0]
    assert item["section"] == "quantity_terms"
    assert item["payload"]["data_source"] == "equipment_assign"
    assert item["payload"]["columns"] == ["EQP_ID"]
    assert item["payload"]["aggregation_method"] == "nunique"
    assert "장비 대수" in item["payload"]["aliases"]
    assert payload["trace"]["worker_rule_recovery"]["status"] == "recovered"


def test_empty_llm_items_do_not_invent_contract_without_dataset_column_and_aggregation():
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": "장비 데이터를 등록해줘."}, "refinement": {}},
        {"items": []},
    )

    assert payload["items"] == []
    assert payload["errors"][0]["type"] == "no_valid_items"


def test_hold_recipe_contains_structured_dependent_selection_activation():
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": "HOLD LOT history and reason lookup rule"}, "refinement": {}},
        {"items": [{"section": "analysis_recipes", "key": "current_hold_lot_selection", "payload": {}}]},
    )

    item = next(item for item in payload["items"] if item["key"] == "current_hold_lot_selection")
    selection = item["payload"]["dependent_selection"]
    assert selection["current_stage"] == "current_selection"
    assert selection["next_stage"] == "history_selection"
    assert {"사유", "코드", "상세", "이력", "시간"}.issubset(
        set(selection["when_question_includes_any"])
    )


def test_worker_only_when_rule_compiles_recipe_activation_without_dataset_forcing():
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": WORKER_EQUIPMENT_RULE}, "refinement": {}},
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "equipment_assignment_uph_join",
                    "payload": {
                        "display_name": "장비 배정과 UPH 결합",
                        "aliases": ["배정 장비 UPH", "장비 UPH"],
                        "source_datasets": ["equipment_assign", "eqp_uph"],
                        "selection_criteria": [
                            "UPH를 함께 물을 때만 장비 UPH 데이터를 추가로 사용한다."
                        ],
                    },
                }
            ]
        },
    )

    item = payload["items"][0]
    criteria = item["payload"]["selection_criteria"]
    assert criteria["required_all_aliases"] == ["UPH"]
    assert "UPH를 함께 물을 때만 장비 UPH 데이터를 추가로 사용하고, 장비 대수나 목록만 물을 때는 장비 Assign 현황 데이터만 사용해." in criteria["rules"]
    assert item["payload"]["source_datasets"] == ["equipment_assign", "eqp_uph"]


def test_worker_restrictive_rule_does_not_attach_to_unrelated_recipe_when_multiple_exist():
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": WORKER_EQUIPMENT_RULE}, "refinement": {}},
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "equipment_assignment_uph_join",
                    "payload": {"aliases": ["장비 UPH"], "selection_criteria": []},
                },
                {
                    "section": "analysis_recipes",
                    "key": "unrelated_recipe",
                    "payload": {"aliases": ["생산 실적"], "selection_criteria": ["기존 규칙"]},
                },
            ]
        },
    )

    by_key = {item["key"]: item for item in payload["items"]}
    assert by_key["equipment_assignment_uph_join"]["payload"]["selection_criteria"]["required_all_aliases"] == ["UPH"]
    assert by_key["unrelated_recipe"]["payload"]["selection_criteria"] == ["기존 규칙"]


def test_worker_explicit_recipe_key_is_applied_only_to_a_single_recipe_update():
    raw_text = """
    기존 장비 배정 UPH 규칙을 수정해줘. 규칙 키: equipment_assignment_uph_join.
    UPH를 함께 물어볼 때만 이 규칙을 사용해.
    """
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": raw_text}, "refinement": {}},
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "model_generated_key",
                    "payload": {"aliases": ["장비 UPH"], "selection_criteria": []},
                }
            ]
        },
    )

    item = payload["items"][0]
    assert item["key"] == "equipment_assignment_uph_join"
    assert item["payload"]["selection_criteria"]["required_all_aliases"] == ["UPH"]


def test_empty_llm_recipe_update_recovers_only_explicit_key_and_worker_activation_rule():
    raw_text = """
    기존 장비 분석 규칙의 사용 조건만 변경해줘. 규칙 키: equipment_assignment_uph_join.
    UPH를 함께 물어볼 때만 이 규칙을 사용해.
    """
    payload = NORMALIZER.normalize_authoring(
        {"request": {"raw_text": raw_text}, "refinement": {}},
        {"items": []},
    )

    assert payload["errors"] == []
    assert payload["trace"]["worker_rule_recovery"]["status"] == "recovered"
    item = payload["items"][0]
    assert item["section"] == "analysis_recipes"
    assert item["key"] == "equipment_assignment_uph_join"
    criteria = item["payload"]["selection_criteria"]
    assert criteria["required_all_aliases"] == ["UPH"]
    assert "UPH를 함께 물어볼 때만 이 규칙을 사용해." in criteria["rules"]
