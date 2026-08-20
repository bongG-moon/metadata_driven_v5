from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


API_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py"
ANSWER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "20_hybrid_answer_builder.py"
MESSAGE_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "21_v2_answer_message_adapter.py"


def _planning_block_payload() -> dict:
    return {
        "answer_message": "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 모델 응답도 사용하지 않았습니다.",
        "analysis": {
            "status": "error",
            "error": {"type": "required_source_retrieval_failed"},
        },
        "execution_gate": {
            "status": "blocked",
            "critical_failures": [
                {
                    "type": "retrieval_job_validation_failed",
                    "validation_errors": [
                        {
                            "type": "metric_dataset_selection_unresolved",
                            "message": "metric과 요청 시점에 맞는 Table Catalog dataset을 하나로 확정할 수 없습니다.",
                        }
                    ],
                }
            ],
        },
        "intent_plan": {
            "decision_reason": [
                "이 값은 API가 trace에서 직접 복사하면 안 됩니다."
            ]
        },
        "trace": {
            "decision_reason": ["trace의 원문은 사용자 API 확인 항목으로 쓰지 않습니다."]
        },
    }


def test_api_projects_only_answer_builder_confirmation_items_for_safe_planning_block():
    api = load_module(API_PATH)
    payload = _planning_block_payload()
    expected_items = [
        "보유 CAPA 계산에는 장비보유댓수와 평균UPH가 필요합니다.",
        "현재 등록된 데이터셋에는 평균UPH를 제공하는 source가 없습니다.",
    ]
    payload["answer_sections"] = {
        "confirmation_required": {
            "title": "확인필요사항",
            "items": deepcopy(expected_items),
        }
    }

    response = api.build_api_response(payload)

    assert response["confirmation_items"] == expected_items
    assert response["answer_sections"]["confirmation_required"] == {
        "title": "확인필요사항",
        "items": expected_items,
    }
    assert "trace의 원문" not in "\n".join(response["confirmation_items"])
    assert "API가 trace" not in "\n".join(response["confirmation_items"])


def test_api_does_not_promote_raw_intent_or_trace_reasons_without_sanitized_section():
    api = load_module(API_PATH)
    payload = _planning_block_payload()

    response = api.build_api_response(payload)

    assert "confirmation_items" not in response


def test_answer_message_and_api_share_the_same_blocked_confirmation_items():
    answer = load_module(ANSWER_PATH)
    message_adapter = load_module(MESSAGE_PATH)
    api = load_module(API_PATH)
    payload = _planning_block_payload()
    expected_items = [
        "보유 CAPA 계산에는 장비보유댓수와 평균UPH가 필요합니다.",
        "현재 메타데이터만으로 평균UPH를 제공하는 데이터셋을 확정할 수 없습니다.",
    ]
    payload["intent_plan"]["decision_reason"] = deepcopy(expected_items)

    answered = answer.build_answer_response(payload)
    message = message_adapter.build_message(answered)
    response = api.build_api_response(answered, message)

    assert answered["answer_sections"]["confirmation_required"]["items"] == expected_items
    assert "### 확인필요사항" in message
    assert response["confirmation_items"] == expected_items
    assert response["message"] == message


def test_api_keeps_nonblocked_response_contract_unchanged_when_it_has_decision_reason():
    api = load_module(API_PATH)
    payload = {
        "analysis": {"status": "ok"},
        "intent_plan": {"decision_reason": ["일반적인 분석 근거입니다."]},
    }

    response = api.build_api_response(payload)

    assert response["status"] == "ok"
    assert "confirmation_items" not in response
