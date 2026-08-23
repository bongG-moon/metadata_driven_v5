from __future__ import annotations

from copy import deepcopy

import pytest

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


@pytest.fixture(scope="module")
def executor():
    return load_module(V2_ROOT / "17_hybrid_analysis_executor.py")


@pytest.fixture(scope="module")
def adapter():
    return load_module(V2_ROOT / "21_v2_answer_message_adapter.py")


def _payload(
    *,
    result_mode: str = "aggregate",
    strict_result_columns: bool = False,
    metric_columns: list[str] | None = None,
    result_columns: list[str] | None = None,
    filter_mappings: dict | None = None,
) -> dict:
    metrics = ["TOTAL_INTERVAL_RATE"] if metric_columns is None else metric_columns
    results = ["OPER_NAME", *metrics] if result_columns is None else result_columns
    return {
        "request": {"question": "generic metric output request"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "generic_source",
                    "source_alias": "source",
                    "filter_mappings": filter_mappings or {"OPER_NAME": ["OPER_NAME"]},
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": result_mode,
                "grain_columns": ["OPER_NAME"],
                "metric_columns": metrics,
                "result_columns": results,
                "strict_result_columns": strict_result_columns,
            },
        },
        "runtime_sources": {"source": [{"OPER_NAME": "D/A1"}]},
        "trace": {"inspection": {}},
    }


def test_missing_aggregate_metric_fails_after_computation_and_recovers_partial_result(
    executor,
    adapter,
):
    executed = executor.execute_pandas_code(
        _payload(result_mode="aggregate", strict_result_columns=False),
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1'}])",
    )

    assert executed["analysis"]["status"] == "partial"
    assert executed["analysis"]["row_count"] == 1
    assert executed["analysis"]["columns"] == ["OPER_NAME"]
    assert executed["analysis"]["error"]["type"] == "output_contract_violation"
    assert "metric" in executed["analysis"]["error"]["message"]
    assert "TOTAL_INTERVAL_RATE" in executed["analysis"]["error"]["message"]
    assert executed["analysis"]["recovered_result"] == {
        "available": True,
        "checkpoint_key": "computed_result",
        "checkpoint_role": "computed_result",
        "row_count": 1,
    }
    assert executed["data"]["partial"] is True
    assert executed["data"]["columns"] == ["OPER_NAME"]
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1"}]
    assert executed["intermediate_results"][0]["role"] == "computed_result"

    message = adapter.build_message(
        executed,
        show_result_table=True,
        show_intermediate_results=True,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )
    assert message.startswith(
        "> ⚠️ **결과 계약 적용 단계에서 오류가 발생했습니다.**"
    )
    assert "직전 정상 단계의 결과 데이터를 기반으로 답변을 생성했습니다." in message
    assert "### 결과 테이블" in message
    assert "OPER_NAME" in message
    assert "D/A1" in message


def test_missing_code_with_only_raw_source_checkpoint_remains_error(executor):
    executed = executor.execute_pandas_code(
        _payload(result_mode="detail", strict_result_columns=False, metric_columns=[]),
        "",
    )

    assert executed["analysis"]["status"] == "error"
    assert executed["analysis"]["error"]["type"] == "missing_code"
    assert not executed["analysis"].get("recovered_result")
    assert not executed.get("data", {}).get("partial")


def test_missing_metric_is_also_checked_for_strict_detail_contract(executor):
    executed = executor.execute_pandas_code(
        _payload(result_mode="detail", strict_result_columns=True),
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1'}])",
    )

    assert executed["analysis"]["status"] == "partial"
    assert executed["data"]["partial"] is True
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1"}]


def test_metric_contract_accepts_source_scoped_physical_equivalent(executor):
    payload = _payload(
        result_mode="aggregate",
        strict_result_columns=True,
        filter_mappings={
            "OPER_NAME": ["OPER_NAME"],
            "TOTAL_INTERVAL_RATE": ["RATE_VALUE"],
        },
    )
    executed = executor.execute_pandas_code(
        payload,
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1', 'RATE_VALUE': 80.0}])",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["columns"] == ["OPER_NAME", "TOTAL_INTERVAL_RATE"]
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "D/A1", "TOTAL_INTERVAL_RATE": 80.0}
    ]


def test_unbound_metric_does_not_accept_physical_alias_shared_by_another_source(executor):
    payload = _payload(result_mode="aggregate", strict_result_columns=False)
    payload["intent_plan"]["retrieval_jobs"] = [
        {
            "dataset_key": "rate_source",
            "source_alias": "rate",
            "filter_mappings": {"TOTAL_INTERVAL_RATE": ["VALUE"]},
        },
        {
            "dataset_key": "qty_source",
            "source_alias": "qty",
            "filter_mappings": {"QTY": ["VALUE"]},
        },
    ]
    payload["runtime_sources"] = {
        "rate": [{"TOTAL_INTERVAL_RATE": 80.0}],
        "qty": [{"QTY": 10}],
    }

    executed = executor.execute_pandas_code(
        payload,
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1', 'VALUE': 10}])",
    )

    assert executed["analysis"]["status"] == "partial"
    assert "TOTAL_INTERVAL_RATE" in executed["analysis"]["error"]["message"]
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1", "VALUE": 10}]


def test_bound_metric_still_rejects_physical_alias_shared_by_another_source(executor):
    payload = _payload(result_mode="aggregate", strict_result_columns=False)
    payload["intent_plan"]["retrieval_jobs"] = [
        {
            "dataset_key": "rate_source",
            "source_alias": "rate",
            "filter_mappings": {"TOTAL_INTERVAL_RATE": ["VALUE"]},
        },
        {
            "dataset_key": "qty_source",
            "source_alias": "qty",
            "filter_mappings": {"QTY": ["VALUE"]},
        },
    ]
    payload["intent_plan"]["output_contract"]["metric_bindings"] = [
        {
            "source_alias": "rate",
            "dataset_key": "rate_source",
            "source_column": "VALUE",
            "output_column": "TOTAL_INTERVAL_RATE",
            "aggregation": "mean",
        }
    ]
    payload["runtime_sources"] = {
        "rate": [{"TOTAL_INTERVAL_RATE": 80.0}],
        "qty": [{"QTY": 10}],
    }

    executed = executor.execute_pandas_code(
        payload,
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1', 'VALUE': 10}])",
    )

    assert executed["analysis"]["status"] == "partial"
    assert "TOTAL_INTERVAL_RATE" in executed["analysis"]["error"]["message"]
    assert executed["analysis"]["recovered_result"]["available"] is True
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1", "VALUE": 10}]


def test_non_strict_detail_keeps_legacy_missing_metric_behavior(executor):
    executed = executor.execute_pandas_code(
        _payload(result_mode="detail", strict_result_columns=False),
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1'}])",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1"}]


def test_aggregate_without_declared_metric_keeps_legacy_behavior(executor):
    payload = _payload(
        result_mode="aggregate",
        strict_result_columns=True,
        metric_columns=[],
        result_columns=["OPER_NAME"],
    )
    executed = executor.execute_pandas_code(
        deepcopy(payload),
        "result = pd.DataFrame([{'OPER_NAME': 'D/A1'}])",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"OPER_NAME": "D/A1"}]
