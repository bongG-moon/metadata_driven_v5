from __future__ import annotations

import pytest

from component_test_support import ROOT, load_module


NORMALIZER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "04_table_catalog_saving_result_normalizer.py"
)
WRITER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "07_table_catalog_review_writer.py"
)


def _authoring_payload(raw_text: str) -> dict:
    return {
        "request": {"raw_text": raw_text, "dry_run": True, "duplicate_action": "skip"},
        "refinement": {
            "refined_text": "",
            "needs_more_input": False,
            "missing_information": [],
            "assumptions": [],
        },
        "errors": [],
        "trace": {},
    }


def _production_candidate(value_transform: object) -> dict:
    return {
        "items": [
            {
                "dataset_key": "production_history",
                "status": "active",
                "payload": {
                    "display_name": "Production History",
                    "dataset_family": "production",
                    "source_type": "oracle",
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "PNT_RPT",
                        "query_template": (
                            "SELECT WORK_DATE, PRODUCTION FROM PROD_TABLE2 "
                            "WHERE WORK_DATE = {DATE}"
                        ),
                    },
                    "required_params": ["DATE"],
                    "required_param_mappings": {"DATE": ["WORK_DATE"]},
                    "filter_mappings": {
                        "DATE": ["WORK_DATE"],
                        "PRODUCTION": ["PRODUCTION"],
                    },
                    "columns": ["WORK_DATE", "PRODUCTION"],
                    "metric_semantics": {
                        "PRODUCTION": {
                            "semantic_type": "quantity",
                            "additive": True,
                            "default_rollup": "sum",
                            "allowed_rollups": ["sum"],
                            "source_already_aggregated": False,
                            "value_transform": value_transform,
                        }
                    },
                },
            }
        ]
    }


@pytest.mark.parametrize(
    "generated_transform",
    [
        {},
        {"coerce_numeric": True},
        {"coerce_numeric": True, "multiplier": None},
    ],
    ids=["empty", "missing_multiplier", "empty_multiplier"],
)
def test_normalizer_discards_unrequested_empty_or_incomplete_value_transform(
    generated_transform: dict,
) -> None:
    normalized = NORMALIZER.normalize_authoring(
        _authoring_payload(
            "PRODUCTION 컬럼을 생산량 수량으로 사용하고 합계로 조회해줘. "
            "DATE는 WORK_DATE에 넣어 조회해."
        ),
        _production_candidate(generated_transform),
    )

    metric = normalized["items"][0]["payload"]["metric_semantics"]["PRODUCTION"]
    assert "value_transform" not in metric

    reviewed = WRITER.review_and_write(normalized)
    assert reviewed["review"]["errors"] == []
    assert reviewed["write_result"]["success"] is True
    assert reviewed["write_result"]["would_save_count"] == 1


def test_prompt_does_not_offer_a_generic_value_transform_example() -> None:
    prompt = (
        ROOT
        / "langflow_components"
        / "table_catalog_saving_flow"
        / "03_saving_prompt_template_ko.md"
    ).read_text(encoding="utf-8")

    assert "원문에 없는 배수는 만들지 않는다" in prompt
    assert '"value_transform": {{' not in prompt
    assert '"multiplier": 1000' not in prompt


def test_normalizer_keeps_explicit_numeric_multiplier_with_unit_conversion_evidence() -> None:
    normalized = NORMALIZER.normalize_authoring(
        _authoring_payload(
            "PRODUCTION 컬럼은 생산량이다. 원본 생산량은 천 단위로 저장되어 있으므로 "
            "조회 전에 숫자로 변환한 뒤 1000을 곱해 실제 생산량으로 환산해."
        ),
        _production_candidate({"coerce_numeric": True, "multiplier": 1000}),
    )

    metric = normalized["items"][0]["payload"]["metric_semantics"]["PRODUCTION"]
    assert metric["value_transform"] == {"coerce_numeric": True, "multiplier": 1000}

    reviewed = WRITER.review_and_write(normalized)
    assert reviewed["review"]["errors"] == []
    assert reviewed["write_result"]["success"] is True


def test_explicit_but_nonnumeric_multiplier_still_reaches_writer_validation() -> None:
    normalized = NORMALIZER.normalize_authoring(
        _authoring_payload(
            "PRODUCTION 컬럼은 생산량이다. 단위 환산을 위해 원본 값에 배수를 적용해야 하지만 "
            "그 multiplier 값은 TBD로 아직 정해지지 않았다."
        ),
        _production_candidate({"coerce_numeric": True, "multiplier": "TBD"}),
    )

    metric = normalized["items"][0]["payload"]["metric_semantics"]["PRODUCTION"]
    assert metric["value_transform"] == {"coerce_numeric": True, "multiplier": "TBD"}

    reviewed = WRITER.review_and_write(normalized)
    assert reviewed["write_result"]["success"] is False
    assert {
        error["type"] for error in reviewed["review"]["errors"]
    } >= {"invalid_metric_value_transform_multiplier"}
