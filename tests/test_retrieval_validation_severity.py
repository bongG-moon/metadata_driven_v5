from __future__ import annotations

from component_test_support import ROOT, load_module


VALIDATOR_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "06_retrieval_job_validator.py"
)
GATE_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "14a_retrieval_execution_gate.py"
)


def _modules():
    return load_module(VALIDATOR_PATH), load_module(GATE_PATH)


def _valid_job() -> dict:
    return {
        "dataset_key": "equipment_assign",
        "source_alias": "equipment_assign_source",
        "source_type": "oracle",
        "required": True,
    }


def _intermediate_metric_issue(*, severity: str | None) -> dict:
    issue = {
        "type": "invalid_metric_source_contract",
        "message": "출력 metric과 조회 source 계약이 일치하지 않습니다.",
        "issues": [
            {
                "output_column": "avg_uph",
                "source_alias": "joined_metrics",
                "issue": "missing_retrieval_job",
            }
        ],
    }
    if severity is not None:
        issue["severity"] = severity
    return issue


def test_warning_plan_validation_keeps_valid_job_and_gate_continues():
    """A proven recoverable plan warning must not discard a safe retrieval job."""

    validator, gate = _modules()
    warning = _intermediate_metric_issue(severity="warning")
    validated = validator.validate_retrieval_payload(
        {
            "intent_plan": {
                "retrieval_jobs": [_valid_job()],
                "validation_errors": [warning],
            }
        }
    )

    jobs = validated["intent_plan"]["retrieval_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["source_alias"] == "equipment_assign_source"
    assert jobs[0]["job_id"] == "job_1"

    validation = validated["trace"]["inspection"]["data_retrieval"]["job_validation"]
    assert validation["input_job_count"] == 1
    assert validation["valid_job_count"] == 1
    assert validation["error_count"] == 0
    assert validation["warning_count"] == 1
    assert validation["errors"] == []
    assert validation["warnings"] == [warning]
    assert warning not in validated["trace"].get("errors", [])
    assert warning in validated["trace"].get("warnings", [])

    continued = gate.apply_retrieval_execution_gate(
        {
            **validated,
            "source_results": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "equipment_assign_source",
                    "source_type": "oracle",
                    "status": "ok",
                    "success": True,
                    "row_count": 2,
                }
            ],
        }
    )

    assert continued["execution_gate"]["status"] == "continue"
    assert continued["execution_gate"]["pandas_execution_allowed"] is True
    assert continued["execution_gate"]["critical_failures"] == []
    assert warning in continued["trace"].get("warnings", [])


def test_plan_validation_without_explicit_warning_severity_still_blocks():
    """Unknown or legacy validation failures remain blocking by default."""

    validator, gate = _modules()
    error = _intermediate_metric_issue(severity=None)
    validated = validator.validate_retrieval_payload(
        {
            "intent_plan": {
                "retrieval_jobs": [_valid_job()],
                "validation_errors": [error],
            }
        }
    )

    validation = validated["trace"]["inspection"]["data_retrieval"]["job_validation"]
    assert validated["intent_plan"]["retrieval_jobs"] == []
    assert validation["valid_job_count"] == 0
    assert validation["error_count"] == 1
    assert validation.get("warning_count", 0) == 0
    assert validation["errors"] == [error]

    blocked = gate.apply_retrieval_execution_gate(validated)
    assert blocked["execution_gate"]["status"] == "blocked"
    assert blocked["execution_gate"]["pandas_execution_allowed"] is False
    assert blocked["analysis"]["status"] == "error"


def test_structurally_invalid_retrieval_job_remains_blocking():
    """Severity support must not weaken retrieval-job structure validation."""

    validator, gate = _modules()
    validated = validator.validate_retrieval_payload(
        {
            "intent_plan": {
                "retrieval_jobs": [
                    {
                        "dataset_key": "equipment_assign",
                        "source_alias": "equipment_assign_source",
                        # source_type is deliberately absent.
                    }
                ],
                "validation_errors": [],
            }
        }
    )

    validation = validated["trace"]["inspection"]["data_retrieval"]["job_validation"]
    assert validated["intent_plan"]["retrieval_jobs"] == []
    assert validation["valid_job_count"] == 0
    assert validation["error_count"] == 1
    assert validation.get("warning_count", 0) == 0
    assert validation["errors"][0]["type"] == "missing_retrieval_job_field"
    assert validation["errors"][0]["field"] == "source_type"

    blocked = gate.apply_retrieval_execution_gate(validated)
    assert blocked["execution_gate"]["status"] == "blocked"
    assert blocked["execution_gate"]["pandas_execution_allowed"] is False
    assert blocked["analysis"]["error"]["type"] == "required_source_retrieval_failed"
