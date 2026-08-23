from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


ANSWER_BUILDER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "20_hybrid_answer_builder.py"
)


def _answer_builder():
    return load_module(ANSWER_BUILDER_PATH)


def _partial_result_payload() -> dict:
    recovered = {
        "available": True,
        "checkpoint_stage": "typed_step",
        "checkpoint_key": "node_groupby_product_count",
        "checkpoint_role": "computed_result",
        "row_count": 1,
        "columns": ["LEAD", "equipment_count", "avg_uph"],
    }
    rows = [{"LEAD": "78", "equipment_count": 2, "avg_uph": 125.0}]
    return {
        "request": {"question": "SBM공정 제품별 보유Capa 알려줘"},
        "simple_analysis_contract": {"route": "complex"},
        "intent_plan": {
            "output_contract": {
                "result_columns": [
                    "LEAD",
                    "equipment_count",
                    "avg_uph",
                    "holding_capacity",
                ],
                "column_labels": {
                    "equipment_count": "장비보유댓수",
                    "avg_uph": "평균UPH",
                    "holding_capacity": "보유CAPA",
                },
            }
        },
        "analysis": {
            "status": "partial",
            "execution_route": "complex",
            "row_count": 1,
            "columns": ["LEAD", "equipment_count", "avg_uph"],
            "recovered_result": deepcopy(recovered),
            "error": {
                "type": "output_contract_violation",
                "message": "최종 결과 컬럼 holding_capacity를 완성하지 못했습니다.",
            },
        },
        "source_results": [
            {
                "source_alias": "equipment_assign_source",
                "dataset_key": "equipment_assign",
                "status": "ok",
                "row_count": 2,
                "columns": ["OPER_NAME", "LEAD", "EQP_ID"],
                "applied_params": {"DATE": "20260822"},
                "pandas_filters": {
                    "OPER_NAME": {
                        "operator": "in",
                        "value": ["SBM1", "SBM2"],
                    },
                    "LEAD": {"operator": "eq", "value": "78"},
                },
            }
        ],
        "runtime_sources": {
            "equipment_assign_source": [
                {"OPER_NAME": "SBM1", "LEAD": "78", "EQP_ID": "S101"},
                {"OPER_NAME": "SBM2", "LEAD": "78", "EQP_ID": "S102"},
            ]
        },
        "data": {
            "partial": True,
            "preview_only": True,
            "recovered_result": deepcopy(recovered),
            "columns": ["LEAD", "equipment_count", "avg_uph"],
            "rows": deepcopy(rows),
            "row_count": 1,
        },
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "fast_path": {
                    "llm_calls": {
                        "intent": 1,
                        "pandas_generation": 1,
                        "repair": 0,
                        "answer": 0,
                    }
                }
            },
        },
    }


def test_partial_result_uses_deterministic_recovery_answer_without_calling_model():
    answer = _answer_builder()
    model_calls: list[str] = []

    result = answer.build_hybrid_answer_response(
        _partial_result_payload(),
        "LLM에 전달될 답변 프롬프트",
        model_invoker=lambda prompt: model_calls.append(prompt) or "LLM이 생성한 최종 CAPA 답변입니다.",
        use_llm_answer=True,
    )

    response_trace = result["trace"]["inspection"]["answer_model_response"]

    assert model_calls == []
    assert "LLM이 생성한" not in result["answer_message"]
    assert any(token in result["answer_message"] for token in ("부분", "직전 정상", "중간"))
    assert response_trace["used"] is False
    assert response_trace["model_called"] is False
    assert any(token in str(response_trace["policy"]).lower() for token in ("partial", "recover"))
    assert result["analysis"]["status"] == "partial"
    assert result["data"]["rows"] == [
        {"LEAD": "78", "equipment_count": 2, "avg_uph": 125.0}
    ]


def test_partial_result_ignores_already_provided_llm_text_and_shows_verified_source_conditions():
    answer = _answer_builder()

    result = answer.build_answer_response(
        _partial_result_payload(),
        "LLM이 보유CAPA를 999999라고 단정한 답변입니다.",
    )
    response_trace = result["trace"]["inspection"]["answer_model_response"]
    table = result["answer_sections"]["result_table"]
    labels = table["column_labels"]
    label_to_key = {label: key for key, label in labels.items()}
    display_labels = [labels.get(column, column) for column in table["display_columns"]]

    assert "999999" not in result["answer_message"]
    assert response_trace["used"] is False
    assert response_trace["ignored"] is True
    assert any(token in str(response_trace["policy"]).lower() for token in ("partial", "recover"))

    assert "No." in display_labels
    assert "DATE" in display_labels
    assert "OPER_NAME (적용 조건)" in display_labels
    assert "LEAD" in display_labels
    assert table["display_rows"][0][label_to_key["DATE"]] == "20260822"
    assert table["display_rows"][0][label_to_key["OPER_NAME (적용 조건)"]] == "IN: SBM1, SBM2"
    assert table["display_rows"][0]["LEAD"] == "78"

    criteria = result["answer_sections"]["applied_criteria"]
    assert criteria["required_params"]["equipment_assign_source"] == {
        "DATE": "20260822"
    }
    assert criteria["analysis_filters"]["equipment_assign_source"]["LEAD"] == {
        "operator": "eq",
        "value": "78",
    }
