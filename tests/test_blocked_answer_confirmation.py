from __future__ import annotations

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


def _modules():
    return (
        load_module(V2_ROOT / "20_hybrid_answer_builder.py"),
        load_module(V2_ROOT / "21_v2_answer_message_adapter.py"),
    )


def _planning_block_payload(*, decision_reasons, critical_failures=None):
    generic_message = (
        "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 "
        "모델 응답도 사용하지 않았습니다."
    )
    return {
        "request": {"question": "SBM공정 제품별 보유Capa 알려줘"},
        "answer_message": generic_message,
        "intent_plan": {"decision_reason": decision_reasons},
        "execution_gate": {
            "status": "blocked",
            "critical_failures": critical_failures
            if critical_failures is not None
            else [
                {
                    "type": "retrieval_job_validation_failed",
                    "validation_errors": [
                        {
                            "type": "process_scope_incomplete",
                            "message": "질문에 명시된 공정 범위가 조회 필터에 반영되지 않았습니다.",
                        }
                    ],
                }
            ],
        },
        "analysis": {
            "status": "error",
            "error": {
                "type": "required_source_retrieval_failed",
                "message": generic_message,
            },
        },
        "data": {"columns": [], "rows": [], "row_count": 0, "data_ref": ""},
        "trace": {
            "inspection": {
                "intent": {
                    "decision_reason": [
                        "inspection fallback은 plan 사유가 없을 때만 표시됩니다."
                    ]
                }
            },
            "warnings": [],
            "errors": [
                {
                    "type": "required_source_retrieval_failed",
                    "message": generic_message,
                }
            ],
        },
    }


def test_planning_block_renders_sanitized_confirmation_required_without_empty_or_duplicate_notice():
    builder, adapter = _modules()
    reasons = [
        "사용자가 요청한 'SBM공정 제품별 보유Capa'는 보유 CAPA와 제품별 집계를 함께 계산해야 합니다.",
        "calc_available_capacity 레시피는 장비보유댓수와 평균UPH 컬럼을 필요로 합니다.\n추가 데이터 소스가 필요합니다.",
        "필수 입력 컬럼이 누락되어 현재 메타데이터만으로는 정확한 보유 CAPA를 산출할 수 없습니다.",
        "장비별 UPH 정보 또는 해당 컬럼이 포함된 테이블을 등록해 주세요.",
        "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 모델 응답도 사용하지 않았습니다.",
        {"password": "must-never-be-rendered"},
    ]

    answered = builder.build_answer_response(
        _planning_block_payload(decision_reasons=reasons),
        "LLM의 임의 답변은 사용하면 안 됩니다.",
    )

    confirmation = answered["answer_sections"]["confirmation_required"]
    assert confirmation["title"] == "확인필요사항"
    assert confirmation["items"] == [
        reasons[0],
        "calc_available_capacity 레시피는 장비보유댓수와 평균UPH 컬럼을 필요로 합니다. 추가 데이터 소스가 필요합니다.",
        reasons[2],
        reasons[3],
    ]
    assert answered["answer_sections"]["notices"] == []
    assert not any(
        item.get("type") == "empty_result"
        for item in answered["answer_sections"]["notices"]
        if isinstance(item, dict)
    )

    # 확인사항은 optional notice를 끈 화면에서도 답변 바로 다음에 표시된다.
    rendered = adapter.build_message(answered, show_notices=False)
    assert "### 답변" in rendered
    assert "### 확인필요사항" in rendered
    assert rendered.index("### 답변") < rendered.index("### 확인필요사항")
    assert "LLM의 임의 답변" not in rendered
    assert "must-never-be-rendered" not in rendered
    assert "### 결과 테이블" not in rendered
    assert "조건에 맞는 결과 행이 없습니다" not in rendered
    assert "### 참고" not in rendered


def test_confirmation_uses_normalized_intent_inspection_only_when_plan_reason_is_absent():
    builder, _ = _modules()
    payload = _planning_block_payload(decision_reasons=[])
    payload["trace"]["decision_reason"] = ["raw top-level trace는 표시하면 안 됩니다."]

    answered = builder.build_answer_response(payload)

    assert answered["answer_sections"]["confirmation_required"]["items"] == [
        "inspection fallback은 plan 사유가 없을 때만 표시됩니다."
    ]


def test_confirmation_reason_is_single_line_bounded_and_omits_sensitive_text():
    builder, adapter = _modules()
    long_reason = "필수 입력 컬럼을 확인해야 합니다.\n" + ("추가 메타데이터가 필요합니다. " * 80)
    answered = builder.build_answer_response(
        _planning_block_payload(
            decision_reasons=[
                "authorization=Bearer do-not-display",
                "mongodb+srv://user:password@host/db",
                long_reason,
            ]
        )
    )

    items = answered["answer_sections"]["confirmation_required"]["items"]
    assert len(items) == 1
    assert "\n" not in items[0]
    assert len(items[0]) <= 500
    assert items[0].endswith("…")
    rendered = adapter.build_message(answered)
    assert "do-not-display" not in rendered
    assert "user:password" not in rendered


def test_confirmation_is_omitted_for_nonplanning_runtime_or_mixed_gate_failures():
    builder, adapter = _modules()
    reasons = ["계획 단계의 설명처럼 보여도 runtime source 오류에는 연결하지 않습니다."]

    not_blocked = _planning_block_payload(decision_reasons=reasons)
    not_blocked["execution_gate"]["status"] = "continue"
    assert "confirmation_required" not in builder.build_answer_response(not_blocked)[
        "answer_sections"
    ]

    runtime_only = _planning_block_payload(
        decision_reasons=reasons,
        critical_failures=[{"type": "source_retrieval_failed"}],
    )
    runtime_answered = builder.build_answer_response(runtime_only)
    assert "confirmation_required" not in runtime_answered["answer_sections"]

    mixed = _planning_block_payload(
        decision_reasons=reasons,
        critical_failures=[
            {
                "type": "retrieval_job_validation_failed",
                "validation_errors": [{"type": "process_scope_incomplete"}],
            },
            {"type": "required_source_result_missing"},
        ],
    )
    mixed_answered = builder.build_answer_response(mixed)
    assert "confirmation_required" not in mixed_answered["answer_sections"]
    assert "### 확인필요사항" not in adapter.build_message(mixed_answered)
