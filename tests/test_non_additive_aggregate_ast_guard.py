from __future__ import annotations

import pytest

from component_test_support import ROOT, load_module


EXECUTOR_PATH = (
    ROOT / "langflow_components" / "data_analysis_flow_v2" / "17_hybrid_analysis_executor.py"
)


def _payload() -> dict:
    return {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "metric_semantics": {
                        "UPH": {
                            "additive": False,
                            "allowed_rollups": ["mean", "min", "max"],
                        }
                    }
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {"metric_columns": ["EQP_ID_CNT", "UPH"]},
        }
    }


@pytest.fixture(scope="module")
def executor():
    return load_module(EXECUTOR_PATH)


def _contract_error(executor, code: str) -> str:
    return executor._metric_semantics_contract_error(_payload(), code)


@pytest.mark.parametrize(
    "code",
    [
        """
result = frame.groupby(['OPER_NAME']).agg({
    'EQP_ID_CNT': 'sum',
    'UPH': 'mean',
}).reset_index()
""",
        """
result = frame.groupby(['OPER_NAME']).agg(
    EQP_COUNT=('EQP_ID_CNT', 'sum'),
    AVG_UPH=('UPH', 'mean'),
).reset_index()
""",
        """
result = frame.groupby(['OPER_NAME']).agg(
    EQP_COUNT=pd.NamedAgg(column='EQP_ID_CNT', aggfunc='sum'),
    AVG_UPH=pd.NamedAgg(column='UPH', aggfunc='mean'),
).reset_index()
""",
    ],
)
def test_non_additive_aggregate_guard_allows_mixed_column_methods(executor, code: str):
    """A separate additive sum must not make UPH='mean' look invalid."""

    assert _contract_error(executor, code) == ""


@pytest.mark.parametrize(
    "code",
    [
        "result = frame.groupby(['OPER_NAME']).agg({'UPH': 'sum'}).reset_index()",
        """
result = frame.groupby(['OPER_NAME']).agg(
    AVG_UPH=pd.NamedAgg(column='UPH', aggfunc='sum'),
).reset_index()
""",
    ],
)
def test_non_additive_aggregate_guard_still_blocks_uph_sum(executor, code: str):
    """The repair must not relax the actual non-additive UPH=sum policy."""

    assert _contract_error(executor, code) == "비가산 metric uph에는 sum 집계를 사용할 수 없습니다."
