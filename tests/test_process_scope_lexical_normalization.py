from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _load_normalizer():
    spec = importlib.util.spec_from_file_location(
        "process_scope_lexical_normalizer",
        NORMALIZER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _catalog_candidates(process_groups: list[dict]) -> dict:
    return {
        "domain_items": process_groups,
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {
                    "columns": ["OPER_NAME", "EQP_ID"],
                    "filter_mappings": {"OPER_NAME": ["OPER_NAME"]},
                },
            }
        ],
    }


def _process_group(key: str, aliases: list[str], processes: list[str]) -> dict:
    return {
        "section": "process_groups",
        "key": key,
        "payload": {
            "aliases": aliases,
            "field": "OPER_NAME",
            "processes": processes,
        },
    }


def test_unique_punctuation_only_process_variant_aligns_to_explicit_group_scope():
    normalizer = _load_normalizer()
    processes = [f"D/A{index}" for index in range(1, 7)]
    candidates = _catalog_candidates(
        [_process_group("DA", ["DA", "DA공정", "D/A공정"], processes)]
    )
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "assign_data",
            "filters": {"OPER_NAME": {"operator": "eq", "value": "DA1"}},
        }
    ]

    normalized_jobs, guard = normalizer._apply_process_group_filter_fields(
        jobs,
        candidates,
        "DA공정 장비들 현재 작업제품들과 가동율현황 조회해줘",
    )

    assert normalized_jobs[0]["filters"]["OPER_NAME"] == {
        "operator": "in",
        "value": processes,
    }
    assert guard["corrections"] == [
        {
            "source_alias": "assign_data",
            "field": "OPER_NAME",
            "correction_type": "specific_process_scope",
            "from_values": ["DA1"],
            "to_values": processes,
        }
    ]
    assert normalizer._validate_process_scope_contract(
        normalized_jobs,
        candidates,
        "DA공정 장비들 현재 작업제품들과 가동율현황 조회해줘",
    )["status"] == "complete"


def test_ambiguous_compact_process_variant_is_not_aligned():
    normalizer = _load_normalizer()
    candidates = _catalog_candidates(
        [
            _process_group("ALPHA", ["ALPHA", "ALPHA공정"], ["D/A1"]),
            _process_group("BETA", ["BETA", "BETA공정"], ["DA-1"]),
        ]
    )
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "assign_data",
            "filters": {"OPER_NAME": {"operator": "eq", "value": "DA1"}},
        }
    ]

    normalized_jobs, guard = normalizer._apply_process_group_filter_fields(
        jobs,
        candidates,
        "ALPHA공정 장비 현황 조회해줘",
    )

    assert normalized_jobs == jobs
    assert guard["corrections"] == []
    scope = normalizer._validate_process_scope_contract(
        normalized_jobs,
        candidates,
        "ALPHA공정 장비 현황 조회해줘",
    )
    assert scope["status"] == "error"
    assert scope["validation_errors"][0]["type"] == "process_scope_incomplete"


def test_compact_process_match_does_not_change_letters_or_digits():
    normalizer = _load_normalizer()
    contracts = normalizer._process_group_contracts(
        _catalog_candidates(
            [_process_group("DA", ["DA", "DA공정"], ["D/A1", "D/A2"])]
        )
    )
    condition = {"operator": "eq", "value": "DA9"}

    assert normalizer._align_requested_process_condition(
        condition,
        contracts,
        ["D/A1", "D/A2"],
    ) == condition
